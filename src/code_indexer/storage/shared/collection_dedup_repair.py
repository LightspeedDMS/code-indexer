"""Metadata-only dedup + renumber repair for legacy SHARDED_JSON collections
(Bug #1502, https://github.com/LightspeedDMS/code-indexer/issues/1502).

Confirmed root cause: ``chunk_index`` (which feeds a point's identity via
``point_id = md5(f"{project_id}_{file_hash}_{chunk_index}")``) used to be
assigned by ``enumerate()`` over the SUBSET of chunks that survived to get
a fresh embedding in a given indexing run, instead of the chunk's fixed
positional index from the chunker (fixed at the source in
``file_chunking_manager.py``). Cache hits (Story #470) and previously-
silently-skipped chunks shifted that subset run-to-run, so the same label
landed on different chunks across runs -- forging colliding point_ids
with different content (two ``vector_*.json`` files, same id, different
content) and/or leaving otherwise-unique records with chunk labels that no
longer match line order.

This module repairs PRE-EXISTING on-disk data written by the buggy code,
purely from metadata already on disk -- NO re-chunking, NO re-embedding,
NO provider calls. Remediated per dual code review (Claude code-reviewer +
Codex, including the Codex delta re-review round):

  0. **Whole-collection identity gate** (H5 foreign identity formats +
     F2 mixed unique_key presence, unified): before touching anything,
     EVERY scanned record (across every raw copy, including duplicates)
     must carry a ``unique_key`` that (a) is a non-empty string, (b)
     parses via :func:`parse_unique_key`, and (c) satisfies
     ``md5(unique_key) == id``. If ANY record in the collection fails
     this -- missing key, foreign/unparseable format, or a self-check
     mismatch -- the repair is a complete NO-OP: the whole collection is
     passed through untouched (INFO log) and migration proceeds exactly
     as it did before this repair existed (its own downstream scan still
     quarantines a genuine duplicate point_id as before, via
     ``DuplicateSourceIdError``).
  1. **Malformed-record pre-check** (H4, hardened for invalid UTF-8): a
     genuinely unreadable/undecodable (including invalid-UTF-8)/missing-
     'id' vector JSON anywhere in the collection fails the WHOLE repair
     loudly, BEFORE any mutation of any OTHER record -- mirrors
     ``IDIndexManager.scan_vectors_for_id_map_verbose``'s own notion of
     "malformed", but checked eagerly here rather than discovered only
     after other records have already been mutated.
  2. **Authoritative HNSW build parameters** (vector dimension, distance
     metric): read ONLY from ``collection_meta.json`` (``hnsw_index.
     vector_dim``/``.space``, falling back to the top-level
     ``vector_size`` field for dimension only) -- NEVER sniffed from
     vector data, NEVER silently defaulted to a hardcoded constant.
     Resolved during planning, BEFORE the marker is ever written, so an
     undeterminable value fails loud pre-mutation.
  3. **Dedup** (Story #1560, superseding the original quarantine-sidecar
     design): for each duplicated point_id, IF ``id_index.bin`` currently
     references one of the copies (== what query hydration serves
     TODAY), that copy is kept and every OTHER copy is DELETED outright.
     IF NO copy is referenced (or a corrupt-index check below is not
     triggered but no entry exists), ALL copies are DELETED
     symmetrically -- no arbitrary winner, no invented identity, no
     manufactured ``chunk_index``. There is no sidecar, no quarantine
     directory, no leftovers: the vector index is a DERIVED artifact,
     the git repository is the source of truth, and a re-index
     regenerates everything a deleted duplicate copy could ever have
     held. Each deletion fsyncs its containing (source) directory for
     durability. The only remaining raise from this step is
     :class:`DedupRepairAmbiguousError`, and ONLY when ``id_index.bin``
     itself is corrupt (fails to LOAD entirely) -- an entry that
     resolves to NEITHER copy is ALSO folded into the ALL-COPIES-DELETED
     case (Story #1560 coordinator review finding R1), since neither
     scanned copy is what index-based hydration serves today either way
     -- see :func:`_plan_dedup`.
  4. **Renumber**: every surviving record is grouped by
     ``(project_id, file_hash)`` (parsed from its ``unique_key``), sorted
     by ``line_start`` (tie-broken by ``line_end`` then old index), and
     assigned a canonical ``0..N-1`` label -- after a real GAP-CONTINUITY
     check (F1): consecutive chunks must overlap or abut
     (``line_start[i+1] <= line_end[i] + 1``, a tolerance PROVEN against
     genuine ``FixedSizeChunker`` output, not guessed). ``id``/
     ``payload.point_id``/``payload.unique_key``/``payload.chunk_index``/
     ``payload.total_chunks`` are rewritten IN PLACE (the filename is
     left untouched -- nothing depends on it). This is an identity
     transform when labels are already correct.
  5. **Rebuild-derived-artifacts** (replaces the retired forward-map
     mechanism -- closes Codex C1/C2/H3): a durable marker file
     (``.dedup-repair-pending``) is written BEFORE the first mutation
     above; ``id_index.bin`` and the HNSW index + ``id_mapping`` are then
     REBUILT FROM SCRATCH from the repaired JSON tree via the existing,
     already-battle-tested ``IDIndexManager.rebuild_from_vectors`` /
     ``HNSWIndexManager.rebuild_from_vectors`` machinery -- never a
     bespoke value-substitution. The collection's recorded
     ``hnsw_index.current_branch`` (Bug #306) is threaded straight
     through to preserve hidden_branches branch-isolation semantics --
     REUSED, never reimplemented. Quarantined losers are physically
     absent from the tree, so they cannot re-appear as a second HNSW
     label for the same id (H3). The marker is deleted only after BOTH
     rebuilds complete. On the NEXT invocation, if the marker still
     exists (a crash happened between its write and its deletion) OR
     this run's own planning found real changes, the rebuilds are
     MANDATORY even if this run's plan is itself an identity transform --
     this converges a crash-interrupted prior run (JSON already
     rewritten, artifacts never rebuilt) to full consistency (C2). A
     clean collection with no marker and nothing to change skips the
     rebuilds entirely (no perf cost).

An empty post-repair JSON tree combined with a stale marker (Codex HIGH
finding, delta re-review) is anomalous -- either a genuinely-always-empty
collection should never carry this marker, or data was unexpectedly lost
mid-repair. This fails loud rather than silently "converging": a bare
``HNSWIndexManager.rebuild_from_vectors`` call against zero files on disk
returns early WITHOUT deleting a pre-existing stale ``hnsw_index.bin``,
which would otherwise leave stale vectors falsely queryable forever.

All detection (identity gate, malformed pre-check, dedup-winner
resolution, unique_key parsing, gap-continuity sanity checking, HNSW
build-parameter resolution) runs in a pure PLANNING phase before any disk
mutation -- an ambiguous/malformed case (including a corrupt
id_index.bin) raises :class:`DedupRepairAmbiguousError`, with the
collection left COMPLETELY untouched and no marker ever written (Messi
Rule #13
anti-silent-failure): never auto-resolved, requires manual review. This
"leaves collection untouched" guarantee applies specifically to exceptions
raised during planning -- once planning succeeds and the apply phase
begins, a crash is handled by the marker-driven convergence mechanism
above, not by an untouched-collection guarantee.

Wired as step 0 of ``consolidate_collection_in_place()`` (the FRESH
migration path only, before the existing scan/build/verify pipeline) --
``collection_migration.py`` owns that wiring, and also owns cleaning up a
stale marker that could otherwise linger unmanaged on the RESUME
(``CHUNKS_DB``) path, where this repair never runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.id_index_manager import (
    CorruptIDIndexError,
    DuplicateSourceIdError,
    IDIndexManager,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout
from code_indexer.storage.shared.hnsw_sync_state import HNSW_SYNC_STATE_FILENAME
from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)

_COLLECTION_META_FILENAME = "collection_meta.json"
_MARKER_FILENAME = ".dedup-repair-pending"

# Codex F1: derived from FixedSizeChunker's real overlap arithmetic (each
# non-final chunk covers [start, start+chunk_size); the next chunk starts
# at start+step_size < start+chunk_size, so its line_start can never
# exceed the previous chunk's line_end -- proven mathematically AND
# verified against genuine chunker output in
# tests/unit/storage/shared/test_collection_dedup_repair_1502.py::
# TestFixedSizeChunkerOverlapDerivesRenumberTolerance). +1 is a small,
# deliberately safe slack margin, not a guess.
_GAP_CONTINUITY_SLACK = 1

# Mirrors IDIndexManager.scan_vectors_for_id_map_verbose's exact
# file-selection skip-list (kept in sync with that method's own literals,
# and with collection_migration.py's independent copy of the same
# constants) -- this module intentionally does not import those private
# names cross-module, matching this codebase's existing convention of
# duplicating this specific, small skip-list rather than coupling to
# another module's private constants.
_CHUNKS_DB_CONTENT_MANIFEST_FILENAME = "chunks_db_content_manifest.json"
_TEMPORAL_BOOKKEEPING_FILENAMES = frozenset(
    {"temporal_structure.json", "temporal_progress.json", "temporal_meta.json"}
)

_MAX_MALFORMED_SAMPLE_SIZE = 5

# Bug #1502 live-staging amendment: cap on how many skipped-group
# file_hash values are carried in DedupRepairResult / logged in the
# single summary WARNING -- mirrors _MAX_MALFORMED_SAMPLE_SIZE's role
# for the malformed-record pre-check.
_MAX_SKIPPED_GROUP_SAMPLE_SIZE = 5


class DedupRepairAmbiguousReason(Enum):
    """Bug #1969 Round 3: distinguishes WHY DedupRepairAmbiguousError was
    raised, so a caller can decide PROGRAMMATICALLY (never by parsing the
    exception's message text) whether automatic escalation is safe. One
    member per raise site in this module.

    Only CORRUPT_ID_INDEX is safe for IDIndexManager.rebuild_from_
    vectors()'s self-heal escalation (Round 3): id_index.bin itself
    failed to load, so _plan_dedup's own winner-lookup has no
    trustworthy reference and cannot pick a per-chunk winner -- but that
    recovery (a whole-file wipe of every implicated file) does not
    depend on id_index.bin at all, so it can proceed safely.

    Every OTHER reason means this repair's own identity/metadata
    assumptions do not hold for this collection's data (a malformed
    record, a foreign/inconsistent unique_key shape, an undeterminable
    HNSW build parameter, an internal invariant violation, or an
    anomalous stale-marker-plus-empty-tree state). Auto-resolving those
    would be genuinely unsafe -- they must continue to propagate as hard
    failures requiring human review, exactly as before this attribute
    existed."""

    #: id_index.bin itself failed to load (_plan_dedup, AC30) -- the ONLY
    #: reason safe for automatic whole-file-wipe escalation.
    CORRUPT_ID_INDEX = "corrupt_id_index"
    #: A genuinely malformed/unreadable/undecodable vector record was
    #: found during the pre-mutation scan.
    MALFORMED_RECORDS = "malformed_records"
    #: A stale crash marker survives alongside a now-empty JSON tree.
    STALE_MARKER_EMPTY_TREE = "stale_marker_empty_tree"
    #: One file group has a mix of records with and without line_start.
    MIXED_LINE_START_PRESENCE = "mixed_line_start_presence"
    #: A survivor passed the whole-collection identity gate but its
    #: unique_key is unparseable during renumber planning -- an internal
    #: invariant violation (the gate itself would have a bug), not a
    #: legitimate foreign-format record.
    INTERNAL_INVARIANT_VIOLATION = "internal_invariant_violation"
    #: collection_meta.json could not be read/parsed to determine the
    #: authoritative HNSW build parameters.
    HNSW_PARAMS_META_UNREADABLE = "hnsw_params_meta_unreadable"
    #: collection_meta.json does not contain a JSON object.
    HNSW_PARAMS_META_NOT_OBJECT = "hnsw_params_meta_not_object"
    #: Neither hnsw_index.vector_dim nor top-level vector_size is a valid
    #: positive integer.
    HNSW_PARAMS_VECTOR_DIM_UNDETERMINABLE = "hnsw_params_vector_dim_undeterminable"
    #: hnsw_index.space is missing or not a recognized distance metric.
    HNSW_PARAMS_SPACE_UNDETERMINABLE = "hnsw_params_space_undeterminable"
    #: Bug #1969 Round 6 (P1-5): an implicated (project_id, file_hash)
    #: group in ``recover_from_corrupt_id_index_by_wiping_files`` has NO
    #: record with a valid, project-confined relative ``payload.path``
    #: (missing, empty, absolute, or a ``..`` path-traversal escape) --
    #: deleting its records would lose the only durably nameable replay
    #: path with zero mutation to show for it.
    UNRESOLVABLE_REPLAY_PATH = "unresolvable_replay_path"


class DedupRepairAmbiguousError(Exception):
    """Raised when a shifted-label file group cannot be safely,
    unambiguously repaired from metadata alone, when a malformed vector
    record is found during the pre-mutation scan, when the collection's
    authoritative HNSW build parameters cannot be determined, or when a
    stale crash marker is found alongside an anomalous empty JSON tree.

    Always raised during the pure-planning phase, before any disk
    mutation and before the crash marker is ever written (or, for the
    stale-marker-plus-empty-tree case, WITHOUT ever touching the
    pre-existing marker) -- the collection is guaranteed to be left
    byte-for-byte untouched. Requires manual operator review; never
    auto-resolved.

    Bug #1969 Round 3: carries a `reason` attribute (a
    DedupRepairAmbiguousReason member) identifying exactly which raise
    site fired -- see that enum's docstring for which single reason is
    safe for automatic escalation and why every other one is not."""

    def __init__(self, message: str, *, reason: DedupRepairAmbiguousReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class DedupRepairResult:
    """Outcome of one :func:`repair_duplicate_and_shifted_points` call."""

    records_scanned: int = 0
    #: Story #1560 AC6: number of DISTINCT point_ids that had more than
    #: one physical record file this pass.
    duplicate_groups: int = 0
    #: Story #1560 AC6: total physical vector record files scanned this
    #: pass (across every point_id, duplicated or not) -- BEFORE this
    #: pass's deletions. Equal to `collection_total`; both names exist
    #: because AC6 and AC13/AC14 (the persisted health-state field) name
    #: them separately, but they are the same measured quantity.
    records_before: int = 0
    #: Story #1560 AC6: total physical copies actually DELETED this pass
    #: -- sum of (copies - 1) for every winner-kept group plus (copies)
    #: for every whole-group-deleted group.
    records_deleted: int = 0
    #: Story #1560 AC1/AC6: duplicate groups where id_index.bin resolved
    #: a winner -- every OTHER copy was deleted, one survivor remains.
    winner_kept_groups: int = 0
    #: Story #1560 AC2/AC6: duplicate groups with NO id_index.bin entry
    #: -- ALL copies were deleted symmetrically, zero survivors.
    whole_group_deleted_groups: int = 0
    #: Story #1560 AC6: alias of `records_before` -- see that field's
    #: docstring.
    collection_total: int = 0
    records_renumbered: int = 0
    #: True unless the whole-collection identity gate rejected this
    #: collection (foreign/missing/self-inconsistent unique_key anywhere)
    #: -- in which case the collection was passed through untouched.
    gate_passed: bool = True
    #: True iff id_index.bin was rebuilt this call (rebuild-derived-
    #: artifacts mechanism; False on the identity fast path).
    id_index_rebuilt: bool = False
    #: True iff the HNSW index + id_mapping were rebuilt this call.
    hnsw_rebuilt: bool = False
    #: Bug #1502 live-staging amendment: count of file groups EXCLUDED
    #: from the renumber plan because they failed a per-group renumber-
    #: safety check (a genuine line gap, or two distinct records sharing
    #: an identical line range). Their existing point_ids/labels are
    #: left untouched -- dedup still applies to them normally, since
    #: dedup is id-keyed and independent of renumbering. This is no
    #: longer a whole-collection failure (see module docstring).
    groups_skipped_renumber: int = 0
    #: Sample of skipped groups' file_hash values (capped at
    #: _MAX_SKIPPED_GROUP_SAMPLE_SIZE), for logging/diagnostics.
    skipped_renumber_file_hashes: List[str] = field(default_factory=list)
    #: Story #1560 AC19: True iff duplicate groups were DETECTED but
    #: their deletion was WITHHELD this call because the caller passed
    #: `deletion_authorized=False` (Story #1460's rollout-safety gate).
    #: When True, ZERO mutation occurred this call -- no marker, no
    #: deletion, no renumbering, no rebuild -- and the caller must NOT
    #: treat this as a completed dedup outcome.
    deletion_gated: bool = False


def parse_unique_key(unique_key: Any) -> Tuple[str, str, int]:
    """Parse a stored ``unique_key`` (``f"{project_id}_{file_hash}_{index}"``)
    back into its three components.

    ``project_id`` may itself contain underscores (defense-in-depth: in
    practice ``FileIdentifier.get_project_identifier()`` normalizes
    underscores to dashes, but this parser does not assume that).
    ``file_hash`` is always of the exact form ``"sha256:<hex...>"`` --
    ``FileIdentifier._get_file_content_hash()`` is the sole producer of
    this field and always emits that prefix. Parsing therefore anchors on
    the RIGHTMOST ``"sha256:"`` occurrence (unambiguous: neither a
    project_id nor an index ever legitimately contains that literal
    substring) for the file_hash boundary, and the RIGHTMOST underscore
    for the trailing integer index.

    Raises:
        ValueError: ``unique_key`` is not a non-empty string, has no
            underscore separator, has no ``"sha256:"``-anchored file_hash,
            or its trailing component is not an integer.
    """
    if not isinstance(unique_key, str) or not unique_key:
        raise ValueError(f"unique_key must be a non-empty str, got {unique_key!r}")

    last_underscore = unique_key.rfind("_")
    if last_underscore == -1:
        raise ValueError(f"unique_key has no separator: {unique_key!r}")

    index_str = unique_key[last_underscore + 1 :]
    try:
        index = int(index_str)
    except ValueError as exc:
        raise ValueError(
            f"unique_key's trailing index component is not an integer: {unique_key!r}"
        ) from exc

    remainder = unique_key[:last_underscore]  # "{project_id}_{file_hash}"

    anchor = "sha256:"
    anchor_pos = remainder.rfind(anchor)
    if anchor_pos == -1:
        raise ValueError(
            f"unique_key does not contain a 'sha256:'-anchored file_hash: {unique_key!r}"
        )
    if anchor_pos == 0 or remainder[anchor_pos - 1] != "_":
        raise ValueError(
            f"unique_key's file_hash anchor is not underscore-delimited: {unique_key!r}"
        )

    project_id = remainder[: anchor_pos - 1]
    file_hash = remainder[anchor_pos:]
    if not project_id:
        raise ValueError(f"unique_key has an empty project_id: {unique_key!r}")

    return project_id, file_hash, index


def _is_vector_record_file(json_file: Path) -> bool:
    if "collection_meta" in json_file.name:
        return False
    if json_file.name == IDIndexManager.INDEX_FILENAME:
        return False
    if json_file.name == _CHUNKS_DB_CONTENT_MANIFEST_FILENAME:
        return False
    if json_file.name in _TEMPORAL_BOOKKEEPING_FILENAMES:
        return False
    if json_file.name == HNSW_SYNC_STATE_FILENAME:
        # Bug #1619: the dedicated hnsw_sync dirty-marker file lives
        # alongside collection_meta.json in the collection root -- it is a
        # bookkeeping sidecar, never a vector record.
        return False
    return True


def _extract_lightweight_identity_fields(
    data: Dict[str, Any], point_id: str
) -> Dict[str, Any]:
    """Bug #1558: extract ONLY the fields the planning phase needs
    (the whole-collection identity gate + renumber-plan grouping/sorting)
    from a freshly-parsed record, discarding everything else -- most
    importantly the embedding ``vector`` and any large payload fields
    (e.g. ``content``/``chunk_text``).

    Profiling (tracemalloc) proved retaining every scanned record's FULL
    parsed JSON for the whole scan -> plan -> apply lifecycle was the
    dominant unbounded in-memory allocation for a large legacy
    collection: 288.8 MB @ N=8000 records, 577.0 MB @ N=16000 -- almost
    exactly linear in N, extrapolating to multi-GB at the 343,604-record
    scale that drove a production worker to 6.6 GB RSS and repeated
    recycling. Letting the caller discard the full parsed ``data`` dict
    immediately (this function returns a small, bounded-size substitute)
    is what actually bounds memory.

    The APPLY phase (see ``repair_duplicate_and_shifted_points``'s
    renumber loop, via ``_read_full_json_record``) re-reads each
    survivor's full record fresh from disk at write time instead of
    reusing a value from this lightweight view, so no information is
    lost -- only its *lifetime* is shortened.

    Bug #1747: also captures PRESENCE-ONLY booleans (``has_path``,
    ``has_content``, ``has_hidden_branches_key`` on the payload view,
    plus a top-level ``chunk_text_is_empty_string``) -- true KEY-PRESENCE
    checks, never value truthiness, and never the actual (possibly
    large) field values -- just enough for
    :func:`_is_hidden_branches_only_bookkeeping_record` to recognize the
    confirmed non-content bookkeeping sentinel shape written by Story
    #339's branch-visibility-hiding path, without re-inflating this
    function's bounded-memory guarantee.
    """
    payload = data.get("payload")
    lightweight_payload: Optional[Dict[str, Any]] = None
    if isinstance(payload, dict):
        lightweight_payload = {
            "unique_key": payload.get("unique_key"),
            "line_start": payload.get("line_start"),
            "line_end": payload.get("line_end"),
            "has_path": "path" in payload,
            "has_content": "content" in payload,
            "has_hidden_branches_key": "hidden_branches" in payload,
            # Bug #1969 Round 4 (R3-F1): the path VALUE (a small, bounded
            # string) -- never a memory concern like the large `content`/
            # `vector` fields Bug #1558 excludes above. Needed by
            # recover_from_corrupt_id_index_by_wiping_files() to surface
            # WHICH relative paths it wiped, so a same-run reprocessing
            # caller can re-queue them.
            "path": payload.get("path"),
        }
    return {
        "id": point_id,
        "payload": lightweight_payload,
        "chunk_text_is_empty_string": data.get("chunk_text") == "",
    }


def _read_full_json_record(json_path: Path) -> Dict[str, Any]:
    """Bug #1558: re-read a record's FULL content (including its
    embedding vector) fresh from disk, for the APPLY phase's write step.

    Safe to call for any SURVIVOR path in
    :func:`repair_duplicate_and_shifted_points`'s renumber loop: the
    dedup-quarantine loop that runs earlier in the same PHASE 2 only
    moves LOSER paths, never a survivor's own path, and the whole
    fresh-path migration runs under the repo's exclusive write lock, so
    nothing else can be mutating this file concurrently.
    """
    with open(json_path) as f:
        record: Dict[str, Any] = json.load(f)
    return record


def _scan_raw_records(
    collection_dir: Path,
) -> Tuple[Dict[str, List[Path]], List[Tuple[Path, str]], Dict[Path, Dict[str, Any]]]:
    """Single pass over every vector record file: builds the tolerant
    point_id -> [paths] map (never raising on a duplicate -- resolving
    that ambiguity is this module's job), collects a (path, reason) entry
    for every MALFORMED record (Codex H4 -- checked eagerly, surfaced by
    the caller BEFORE any mutation of any other record), and caches a
    LIGHTWEIGHT identity view of every successfully-parsed record by path
    (Bug #1558: never the full record -- see
    :func:`_extract_lightweight_identity_fields`).

    "Malformed" mirrors IDIndexManager.scan_vectors_for_id_map_verbose's
    own notion (JSON parse error, non-dict JSON, or a missing/invalid
    'id' field), PLUS Codex finding 3: any read/decode failure -- a
    genuinely invalid-UTF-8 file raises ``UnicodeDecodeError`` from
    ``json.load()`` (a ``ValueError`` subclass, distinct from
    ``json.JSONDecodeError``) which must be classified as malformed too,
    never allowed to escape this collector as an unhandled exception.
    """
    id_to_paths: Dict[str, List[Path]] = {}
    malformed: List[Tuple[Path, str]] = []
    identity_by_path: Dict[Path, Dict[str, Any]] = {}

    for json_file in collection_dir.rglob("*.json"):
        if not _is_vector_record_file(json_file):
            continue
        try:
            with open(json_file) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError AND UnicodeDecodeError
            # (raised by json.load()'s internal f.read() on invalid UTF-8)
            # -- both are genuinely malformed/unreadable records.
            malformed.append((json_file, f"unreadable/undecodable: {exc}"))
            continue
        if not isinstance(data, dict):
            malformed.append(
                (json_file, f"expected a JSON object, got {type(data).__name__}")
            )
            continue
        point_id = data.get("id")
        if not isinstance(point_id, str) or not point_id:
            malformed.append((json_file, f"missing/invalid 'id' field: {point_id!r}"))
            continue

        # Bug #1558: retain only the lightweight identity view -- `data`
        # (which may hold a large embedding vector + payload content)
        # goes out of scope at the end of this iteration and is freed
        # immediately, rather than being kept alive for the whole scan.
        identity_by_path[json_file] = _extract_lightweight_identity_fields(
            data, point_id
        )
        id_to_paths.setdefault(point_id, []).append(json_file)

    return id_to_paths, malformed, identity_by_path


def _record_has_self_consistent_identity(record: Dict[str, Any]) -> bool:
    """Codex H5 + Claude F2, unified whole-collection identity gate
    predicate: True iff `record`'s payload carries a unique_key that is a
    non-empty string, parses via :func:`parse_unique_key`, and whose
    md5 equals the record's own stored point_id (``id``). False for
    ANY other shape -- missing key, non-string, unparseable/foreign
    format, or a self-check mismatch."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    unique_key = payload.get("unique_key")
    if not isinstance(unique_key, str) or not unique_key:
        return False
    try:
        parse_unique_key(unique_key)
    except ValueError:
        return False
    point_id = record.get("id")
    if not isinstance(point_id, str):
        return False
    return hashlib.md5(unique_key.encode()).hexdigest() == point_id


def _is_hidden_branches_only_bookkeeping_record(record: Dict[str, Any]) -> bool:
    """Bug #1747: True iff `record` -- a LIGHTWEIGHT identity view
    produced by :func:`_extract_lightweight_identity_fields` (never the
    raw parsed JSON) -- matches the EXACT confirmed production shape of
    the non-content bookkeeping sentinel written/touched by Story #339's
    branch-visibility-hiding path (``FilesystemVectorStore.
    _batch_update_payload_only`` / ``_batch_update_points``): its
    payload carries a ``hidden_branches`` key and NOTHING else identity/
    content-relevant (no ``path``, no ``content``, no real
    ``unique_key``), and the record's top-level ``chunk_text`` was the
    empty string. Confirmed live on 4 production repos (65 affected
    records): payload=={"hidden_branches": [...]}, chunk_text=="".

    `_extract_lightweight_identity_fields` never retains the raw
    ``path``/``content``/``chunk_text`` values (Bug #1558's bounded-
    memory contract) -- this predicate therefore reads its PRESENCE-only
    substitutes (``has_path``, ``has_content``, ``has_hidden_branches_
    key`` on the payload sub-dict, ``chunk_text_is_empty_string`` at the
    top level) rather than the original field names. Note the
    ``unique_key`` check below is deliberately a VALUE check
    (``not in (None, "")``), not a key-membership check: the lightweight
    payload view unconditionally sets ``payload["unique_key"]`` for
    every dict payload (None when the original had none), so a
    membership check would always be True and this predicate would
    never match anything.

    Deliberately narrow -- every condition must hold, so a record that
    merely lacks a ``unique_key`` (genuine Bug #1579 territory) is never
    misclassified as this bookkeeping shape. False for anything that
    isn't an exact match, including a record carrying ``hidden_branches``
    ALONGSIDE a real ``path``/``content`` field (mis-tagged real content,
    not pure bookkeeping) or non-empty ``chunk_text``."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    if not payload.get("has_hidden_branches_key"):
        return False
    if payload.get("has_path"):
        return False
    if payload.get("has_content"):
        return False
    if payload.get("unique_key") not in (None, ""):
        return False
    return record.get("chunk_text_is_empty_string") is True


def _whole_collection_identity_gate_passes(
    id_to_paths: Dict[str, List[Path]], identity_by_path: Dict[Path, Dict[str, Any]]
) -> bool:
    """Runs the identity-consistency predicate over EVERY raw record
    (including every copy of a duplicated point_id) -- a single failure
    anywhere means the whole collection does not use this repair's
    identity scheme, and the whole collection must pass through
    untouched.

    Bug #1747: a record matching the confirmed hidden_branches-only
    bookkeeping sentinel shape (see
    :func:`_is_hidden_branches_only_bookkeeping_record`) is SKIPPED here
    -- its missing unique_key is never counted as a gate-failure trigger
    -- rather than rejecting the whole collection over a non-content
    record this repair was never meant to act on. A genuinely bad
    CONTENT record (missing/foreign/self-inconsistent unique_key) still
    fails the gate exactly as before (Bug #1579, unweakened)."""
    for paths in id_to_paths.values():
        for path in paths:
            record = identity_by_path[path]
            if _is_hidden_branches_only_bookkeeping_record(record):
                continue
            if not _record_has_self_consistent_identity(record):
                return False
    return True


def _marker_path(collection_dir: Path) -> Path:
    return collection_dir / _MARKER_FILENAME


def _fsync_dir(dir_path: Path) -> None:
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_marker_durably(collection_dir: Path) -> None:
    """Durably write the crash marker BEFORE any mutation -- temp file in
    the SAME directory, flush+fsync, os.replace, then an nfs_safe_fsync of
    the containing directory (Bug #1407 pattern). A crash anywhere after
    this call leaves durable evidence that the rebuild step is mandatory
    on the next invocation."""
    marker_path = _marker_path(collection_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                json.dump({"pending_since": time.time()}, tmp_f)
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(marker_path))
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
    _fsync_dir(collection_dir)


def _delete_marker_durably(collection_dir: Path) -> None:
    """Remove the crash marker (idempotent) and fsync the containing
    directory so the removal itself survives a crash. Called ONLY after
    BOTH derived-artifact rebuilds have completed successfully."""
    marker_path = _marker_path(collection_dir)
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass
    _fsync_dir(collection_dir)


_OUTCOME_FILENAME = ".dedup-outcome-pending"


def _pending_outcome_path(collection_dir: Path) -> Path:
    return collection_dir / _OUTCOME_FILENAME


def _write_pending_outcome_durably(collection_dir: Path, data: Dict[str, Any]) -> None:
    """Story #1560 AC22: durably write the dedup-outcome journal, same
    atomic pattern as `_write_marker_durably` (temp file in the SAME
    directory, flush+fsync, os.replace, then an nfs_safe_fsync of the
    containing directory)."""
    outcome_path = _pending_outcome_path(collection_dir)
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
            os.replace(tmp_path, str(outcome_path))
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
    _fsync_dir(collection_dir)


def read_pending_dedup_outcome(
    collection_dir: "Path | str",
) -> Optional[Dict[str, Any]]:
    """Story #1560 AC22/AC23: read the crash-durable dedup-outcome
    journal for `collection_dir`, or None if absent (no unresolved
    dedup outcome is waiting to be persisted). Never mutates. Callers
    (the fleet-migration persistence layer) must attempt to record this
    to the durable per-repo state and, ONLY on success, call
    `clear_pending_dedup_outcome` -- never assume it was already
    recorded merely because it exists."""
    outcome_path = _pending_outcome_path(Path(collection_dir))
    try:
        with open(outcome_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "read_pending_dedup_outcome: %s exists but could not be read/"
            "parsed (%s) -- treating as absent (best-effort recovery; a "
            "genuinely unreadable journal cannot be replayed anyway)",
            outcome_path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def clear_pending_dedup_outcome(collection_dir: "Path | str") -> bool:
    """Story #1560 AC22/AC23: remove the dedup-outcome journal (idempotent)
    and fsync the containing directory. Callers must call this ONLY
    AFTER the outcome has been successfully persisted to the durable
    per-repo state -- clearing it any earlier would lose the audit trail
    on a crash. Returns True iff a journal was actually present and
    removed."""
    collection_dir = Path(collection_dir)
    outcome_path = _pending_outcome_path(collection_dir)
    try:
        outcome_path.unlink()
    except FileNotFoundError:
        return False
    _fsync_dir(collection_dir)
    return True


# Bug #1969 Round 5 (R4-F1, P2 BLOCKING): durable, crash-safe replacement
# for Round 4's in-memory-only FilesystemVectorStore._pending_self_heal_
# reprocess_paths queue. That queue was populated ad hoc by whichever
# caller happened to trigger a wipe and was drained by exactly ONE code
# path (smart_indexer.py's _do_incremental_index post-pass drain) --
# every OTHER trigger site (end_indexing's finally block, upsert_points
# during a resumed run, scroll_points under a non-git detect_deletions
# reconcile) populated it too, but nothing ever drained it there, AND the
# in-memory queue itself is lost on process exit regardless. Recording
# here instead -- INSIDE recover_from_corrupt_id_index_by_wiping_files(),
# the SINGLE choke point every self-heal call site already funnels
# through -- guarantees the record exists regardless of which caller
# triggered the wipe, and a fsynced sidecar file survives a process
# crash/restart. Mirrors this module's own established durable-marker
# pattern (`.dedup-repair-pending`, `.dedup-outcome-pending`) -- this is
# operational bookkeeping, not a new "setting".
#
#: Bug #1969 Round 6 (P1-2, Amendment 6): a lock-free, per-event marker
#: directory. Amendment 6 disproved Lead 1 (cluster job dedup does NOT
#: serialize writers of one collection across nodes -- filed separately as
#: issue #1970, out of #1969's scope) and adopted Lead 2: the sidecar
#: itself must be correct with ZERO mutual exclusion available, since
#: NFSv3 `nolock` gives no cross-node lock and no cross-node writer gate
#: exists. Each self-heal wipe event gets its OWN marker file
#: (name = sha256(path) + a random nonce, so two concurrent events for the
#: SAME path never collide), created via a single atomic rename -- there
#: is no shared file to read-merge-write, so there is nothing for two
#: concurrent writers to race on, with or without a lock. This is the
#: ONLY sidecar storage format: a single-file `.self-heal-reprocess-
#: pending` design was drafted for Round 5 but NEVER SHIPPED (Amendment 8
#: -- verified absent from every remote branch and every real index on
#: this machine), so there is no legacy format and no migration.
SELF_HEAL_REPROCESS_PENDING_DIR_NAME = ".self-heal-reprocess-pending.d"


def _self_heal_reprocess_pending_dir(collection_dir: Path) -> Path:
    return collection_dir / SELF_HEAL_REPROCESS_PENDING_DIR_NAME


def _write_self_heal_reprocess_marker(collection_dir: Path, rel_path: str) -> None:
    """Atomically create ONE new marker file recording `rel_path` as
    pending. Never reads or overwrites any other marker -- two concurrent
    calls (same or different processes, same or different paths) each
    create their OWN file and cannot collide or lose each other's write.
    """
    marker_dir = _self_heal_reprocess_pending_dir(collection_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_name = f"{hashlib.sha256(rel_path.encode()).hexdigest()}.{uuid.uuid4().hex}"
    marker_path = marker_dir / marker_name
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(marker_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                tmp_f.write(rel_path)
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(marker_path))
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
    _fsync_dir(marker_dir)


def _is_valid_self_heal_marker_content(marker_path: Path, content: str) -> bool:
    """Bug #1969 Round 6 (P1-4, Amendment 9): true when `content` is
    trustworthy for `marker_path` -- its sha256 digest matches the digest
    embedded in the marker's own filename (`_write_self_heal_reprocess_
    marker` always names a marker after ITS OWN content's digest, so a
    mismatch proves the bytes were damaged after the fact, not a
    legitimate marker for a different path), AND it independently passes
    the same project-confinement check P1-5 already applies at record
    time (`_is_project_confined_relative_path`) -- a marker is never
    trusted as a replay path just because some record-time check once
    passed for different bytes.
    """
    if not _is_project_confined_relative_path(content):
        return False
    expected_digest = marker_path.name.split(".", 1)[0]
    return hashlib.sha256(content.encode()).hexdigest() == expected_digest


def _read_self_heal_reprocess_markers(collection_dir: Path) -> Dict[Path, str]:
    """Read every marker file's recorded path, tolerating one bad marker
    without losing the rest -- an unreadable, malformed, or digest-
    mismatched individual marker is skipped with a WARNING (full P1-4
    quarantine of a marker happens separately, in
    ``quarantine_corrupt_self_heal_reprocess_sidecar``). Returns
    {marker_file_path: rel_path}."""
    marker_dir = _self_heal_reprocess_pending_dir(collection_dir)
    result: Dict[Path, str] = {}
    try:
        entries = list(marker_dir.iterdir())
    except FileNotFoundError:
        return result
    for marker_path in entries:
        if marker_path.suffix == ".tmp":
            continue
        try:
            content = marker_path.read_text().strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "self-heal-reprocess marker %s could not be read (%s) -- "
                "skipping this marker only",
                marker_path,
                exc,
            )
            continue
        if content and _is_valid_self_heal_marker_content(marker_path, content):
            result[marker_path] = content
    return result


def _find_corrupt_self_heal_reprocess_markers(collection_dir: Path) -> List[Path]:
    """Bug #1969 Round 6 (P1-4, Amendment 9): find per-event markers that
    exist but are corrupt -- unreadable (including containing bytes that
    are not valid UTF-8), empty, or holding content whose sha256 digest
    does not match the digest embedded in the marker's own filename (the
    atomic writer always names a marker after its OWN content's digest,
    so a mismatch, like an empty or unreadable marker, means the file was
    damaged after the fact). ``_read_self_heal_reprocess_markers`` silently
    drops such a marker (it only cares about the paths it CAN trust),
    which would permanently lose that file's only replay record.
    Already-quarantined markers (name contains ``.corrupt.``, this
    function's own prior output) are excluded so a marker is quarantined
    exactly once, never re-detected and re-quarantined on every
    subsequent run.
    """
    marker_dir = _self_heal_reprocess_pending_dir(collection_dir)
    corrupt: List[Path] = []
    try:
        entries = list(marker_dir.iterdir())
    except FileNotFoundError:
        return corrupt
    for marker_path in entries:
        if marker_path.suffix == ".tmp" or ".corrupt." in marker_path.name:
            continue
        try:
            content = marker_path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            corrupt.append(marker_path)
            continue
        if not content or not _is_valid_self_heal_marker_content(marker_path, content):
            corrupt.append(marker_path)
    return corrupt


def read_pending_self_heal_reprocess_paths(
    collection_dir: "Path | str",
) -> FrozenSet[str]:
    """Read the durable self-heal reprocess sidecar for `collection_dir`
    -- the union of every Round 6 per-event marker's recorded path. Never
    mutates. A bad individual marker is skipped, not fatal to the rest."""
    collection_dir = Path(collection_dir)
    return frozenset(_read_self_heal_reprocess_markers(collection_dir).values())


def record_self_heal_reprocess_pending(
    collection_dir: "Path | str", paths: FrozenSet[str]
) -> None:
    """Durably record `paths` as needing reprocessing -- each path gets
    its OWN independent marker file (Bug #1969 Round 6 P1-2, Amendment 6),
    so two wipes recorded before anything drains either one never lose
    the first one's paths, and neither can two CONCURRENT recorders lose
    each other's write. No-op for an empty `paths`.

    CONCURRENCY INVARIANT (Bug #1969 Round 6, P1-2, Amendment 6): earlier
    designs considered a shared-file read-merge-write, serialized first by
    ``self._id_index_lock``/``IndexingLock`` (both disproven -- see git
    history), then a same-node file lock (which cannot help on the
    cluster's NFSv3 ``nolock`` mount with no cross-node writer gate --
    Amendment 6 disproved that gate too, filed separately as issue
    #1970). This function has NO read-merge-write cycle AT ALL: each call
    creates one brand-new marker file per path via
    ``_write_self_heal_reprocess_marker`` and never reads or overwrites
    any other marker, so there is nothing for two concurrent recorders
    (same process, different processes, or different NODES) to race on --
    correct with zero mutual exclusion.
    """
    if not paths:
        return
    collection_dir = Path(collection_dir)
    for rel_path in paths:
        _write_self_heal_reprocess_marker(collection_dir, rel_path)


def clear_self_heal_reprocess_paths(
    collection_dir: "Path | str", paths: Iterable[str]
) -> None:
    """Remove `paths` from the durable pending set -- callers MUST call
    this ONLY after those paths have been successfully (not merely
    attempted, and not cancelled) reprocessed; clearing any earlier would
    lose the record on a crash before reprocessing actually finished.
    Idempotent. A no-op (including when nothing is pending) when nothing
    changes.

    Bug #1969 Round 6 (P1-2, Amendment 6): deletes every per-event marker
    file (`_write_self_heal_reprocess_marker`'s output) whose recorded
    path is in `paths` -- best-effort per file, tolerating a marker
    already removed by a concurrent clear. The clear-vs-record race PROOF
    (a marker recorded by another writer AFTER this clear listed its
    markers must never be touched) is a separate, deferred behavior --
    this call only reads the marker directory ONCE per invocation and
    only ever deletes files it saw containing a path this caller asked to
    clear, never anything it didn't enumerate.
    """
    collection_dir = Path(collection_dir)
    to_clear = set(paths)
    if not to_clear:
        return

    for marker_path, rel_path in _read_self_heal_reprocess_markers(
        collection_dir
    ).items():
        if rel_path in to_clear:
            try:
                marker_path.unlink()
            except FileNotFoundError:
                pass


def is_self_heal_reprocess_sidecar_corrupt(collection_dir: "Path | str") -> bool:
    """Bug #1969 Round 6 (P1-4): true when at least one Round 6 per-event
    marker exists but is unreadable or empty (see
    ``_find_corrupt_self_heal_reprocess_markers``). Distinct from a
    genuinely ABSENT/clean sidecar (no self-heal has ever fired, or its
    reprocessing was already fully cleared), which returns False here.

    ``read_pending_self_heal_reprocess_paths`` cannot distinguish "empty
    because nothing is pending" from "empty because a marker is corrupt"
    -- both can return ``frozenset()``. This function is the
    discriminator callers use to decide whether they must fall back to a
    broader detection strategy: Bug #1969 Round 6 P1-4 forces the run
    into reconcile-with-database mode when this returns True, since
    ``_do_reconcile_with_database`` classifies any file with zero points
    as missing and reindexes it -- a complete superset of whatever the
    lost marker might have listed (a self-heal-wiped file has zero
    points regardless of whether its marker survives).
    """
    return bool(_find_corrupt_self_heal_reprocess_markers(Path(collection_dir)))


def _quarantine_one_corrupt_file(source_path: Path) -> Optional[Path]:
    """Move `source_path` aside to `<name>.corrupt.<timestamp>` in the same
    directory (never delete), with a numeric suffix on collision. Returns
    the destination, or None if the move failed (source untouched)."""
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    quarantine_path = source_path.parent / f"{source_path.name}.corrupt.{timestamp}"
    suffix = 0
    while quarantine_path.exists():
        suffix += 1
        quarantine_path = (
            source_path.parent / f"{source_path.name}.corrupt.{timestamp}.{suffix}"
        )
    try:
        os.replace(str(source_path), str(quarantine_path))
    except OSError as exc:
        logger.error(
            "Bug #1969 Round 6 (P1-4): failed to quarantine corrupt "
            "self-heal-reprocess file %s -> %s (%s) -- leaving it in "
            "place.",
            source_path,
            quarantine_path,
            exc,
        )
        return None
    _fsync_dir(source_path.parent)
    return quarantine_path


def quarantine_corrupt_self_heal_reprocess_sidecar(
    collection_dir: "Path | str",
) -> Optional[Path]:
    """Bug #1969 Round 6 (P1-4): move every CORRUPT Round 6 per-event
    marker aside for forensics -- NEVER deletes any of them -- after a
    reconcile-with-database run forced specifically because the sidecar
    could not be trusted completes SUCCESSFULLY. Each marker found
    corrupt (empty or unreadable) by
    ``_find_corrupt_self_heal_reprocess_markers`` is quarantined
    independently, so one corrupt marker never blocks the others' valid
    replay paths from being read normally. Returns the first corrupt
    marker's quarantine destination, or None if nothing needed
    quarantining or every move failed (the corrupt artifact stays in
    place either way -- never lost).
    """
    collection_dir = Path(collection_dir)
    marker_quarantine_path: Optional[Path] = None
    for marker_path in _find_corrupt_self_heal_reprocess_markers(collection_dir):
        result = _quarantine_one_corrupt_file(marker_path)
        if marker_quarantine_path is None:
            marker_quarantine_path = result

    return marker_quarantine_path


def _accumulate_pending_outcome_durably(
    collection_dir: Path, new_counts: Dict[str, int]
) -> None:
    """Story #1560 AC22: fold THIS pass's counts into any pre-existing
    journal before writing it back, IF AND ONLY IF that existing
    journal's `phase` is already "completed" -- a genuinely confirmed
    prior outcome (filesystem-proven, see
    `_mark_pending_outcome_completed_durably`) not yet persisted to the
    DB. Mirrors the durable-state DB layer's own cumulative-vs-snapshot
    semantics (`sqlite_backends.py`'s `_apply_dedup_outcome_upsert`) for
    that safe case: `duplicate_groups`/`records_deleted`/
    `winner_kept_groups`/`whole_group_deleted_groups` are ADDED to the
    existing journal's totals; `records_before`/`collection_total` are
    OVERWRITTEN to this pass's snapshot.

    Codex review Finding F1: an existing journal that is NOT
    "completed" (absent, or `phase == "pending"` from a possibly-
    interrupted earlier attempt whose deletion may never have started
    or finished) must NEVER be added onto -- doing so would double-
    count once the interrupted work is retried and genuinely completes.
    This pass's fresh, accurate recomputation SUPERSEDES it entirely
    instead. Every write from this function is tagged
    `phase: "pending"`, since a NEW deletion attempt is about to begin;
    the caller flips it to "completed" only after filesystem state
    proves the deletion actually finished."""
    existing = read_pending_dedup_outcome(collection_dir) or {}
    existing_is_confirmed_complete = existing.get("phase") == "completed"
    merged: Dict[str, Any]
    if existing_is_confirmed_complete:
        merged = {
            "duplicate_groups": int(existing.get("duplicate_groups", 0))
            + new_counts["duplicate_groups"],
            "records_deleted": int(existing.get("records_deleted", 0))
            + new_counts["records_deleted"],
            "winner_kept_groups": int(existing.get("winner_kept_groups", 0))
            + new_counts["winner_kept_groups"],
            "whole_group_deleted_groups": int(
                existing.get("whole_group_deleted_groups", 0)
            )
            + new_counts["whole_group_deleted_groups"],
            "records_before": new_counts["records_before"],
            "collection_total": new_counts["collection_total"],
        }
    else:
        merged = dict(new_counts)
    merged["phase"] = "pending"
    _write_pending_outcome_durably(collection_dir, merged)


def _mark_pending_outcome_completed_durably(collection_dir: Path) -> None:
    """Codex review Finding F1: flip the pending-outcome journal's
    `phase` to "completed" -- called ONLY after filesystem state PROVES
    the recorded intent actually happened (the deletion loop AND the
    derived-artifact rebuild both finished successfully). A journal
    already "completed" (e.g. a prior crash between this flip and the
    marker delete) is idempotently rewritten unchanged. A genuinely
    absent journal is a no-op -- a renumber-only pass with zero
    duplicates never wrote one."""
    existing = read_pending_dedup_outcome(collection_dir)
    if existing is None:
        return
    if existing.get("phase") == "completed":
        return
    existing["phase"] = "completed"
    _write_pending_outcome_durably(collection_dir, existing)


def collection_has_duplicate_point_ids(collection_dir: "Path | str") -> bool:
    """Story #1560 AC12: cheap, READ-ONLY predicate -- True iff
    `collection_dir` currently has at least one duplicated point_id THAT
    THIS REPAIR WOULD ACTUALLY AUTO-RESOLVE.

    Deliberately NOT "any duplicate the raw scan finds" -- a collection
    whose duplicate uses a FOREIGN/inconsistent identity scheme fails
    the whole-collection identity gate, so `repair_duplicate_and_
    shifted_points` passes it through UNTOUCHED and the pre-existing
    scan (`IDIndexManager.scan_vectors_for_id_map_verbose`) still raises
    `DuplicateSourceIdError` identically on every future attempt --
    resetting quarantine for that case would just cause an immediate
    re-failure, wasting a scheduling attempt for nothing. Reuses this
    module's OWN `_scan_raw_records` + `_whole_collection_identity_gate_
    passes` (the exact predicates `repair_duplicate_and_shifted_points`
    itself uses to decide whether it can act) rather than a second,
    looser detection mechanism -- so this function's answer is always
    consistent with what repair would actually do.

    False for malformed records, an empty collection, or a gate
    failure (foreign scheme) -- True only when the gate passes AND at
    least one point_id has more than one physical copy. Performs zero
    disk mutation.
    """
    collection_dir = Path(collection_dir)
    id_to_paths, malformed, identity_by_path = _scan_raw_records(collection_dir)
    if malformed or not id_to_paths:
        return False
    if not _whole_collection_identity_gate_passes(id_to_paths, identity_by_path):
        return False
    return any(len(paths) > 1 for paths in id_to_paths.values())


def collection_has_any_duplicate_point_ids(collection_dir: "Path | str") -> bool:
    """Bug #1579: cheap, READ-ONLY predicate -- True iff `collection_dir`
    currently has AT LEAST ONE duplicated point_id on disk, PERIOD --
    regardless of whether repair_duplicate_and_shifted_points' whole-
    collection identity gate would currently act on it.

    Deliberately BROADER than :func:`collection_has_duplicate_point_ids`,
    which is scoped to "duplicates THIS REPAIR WOULD AUTO-RESOLVE" (i.e.
    the gate must also pass). This function answers a different question:
    "are there ANY duplicate point_ids on disk right now", which is what
    Bug #1579's quarantine-reset fix needs -- a gate-rejected collection
    that genuinely has duplicates is exactly the case
    ``collection_has_duplicate_point_ids`` cannot see (it returns False
    for ANY gate failure), permanently defeating the quarantine auto-reset
    for that collection even though duplicates are still present.

    Still False for malformed records or an empty collection (the scan
    itself found nothing trustworthy to judge). Performs zero disk
    mutation.
    """
    collection_dir = Path(collection_dir)
    id_to_paths, malformed, _identity_by_path = _scan_raw_records(collection_dir)
    if malformed or not id_to_paths:
        return False
    return any(len(paths) > 1 for paths in id_to_paths.values())


def clear_stale_repair_marker(collection_dir: Path) -> bool:
    """Idempotently remove a stale ``.dedup-repair-pending`` marker if
    present. Intended for ``consolidate_collection_in_place``'s RESUME
    (``CHUNKS_DB``) path, where :func:`repair_duplicate_and_shifted_points`
    itself never runs -- a marker surviving from an OLDER, pre-remediation
    partial run would otherwise linger unmanaged forever on that path.
    Returns True iff a marker was actually present and removed."""
    collection_dir = Path(collection_dir)
    if not _marker_path(collection_dir).exists():
        return False
    _delete_marker_durably(collection_dir)
    return True


def _delete_loser(loser_path: Path) -> None:
    """Story #1560 AC1/AC2/AC4: permanently DELETE a dedup loser (or an
    unresolvable duplicate's copy) -- never move it anywhere. The vector
    index is a derived artifact; the git repository is the source of
    truth, and a re-index regenerates everything a deleted copy could
    ever have held.

    Fsyncs the file's containing (source) directory afterward for
    durability -- there is no destination directory to fsync anymore,
    since nothing is moved."""
    source_dir = loser_path.parent
    loser_path.unlink()
    _fsync_dir(source_dir)


def _atomic_write_json_record(target_path: Path, record: Dict[str, Any]) -> None:
    """Atomic + durable rewrite of one vector_*.json record IN PLACE
    (temp file in the SAME directory, flush+fsync, os.replace, then an
    nfs_safe_fsync of the containing directory) -- mirrors
    collection_migration.py's own ``_atomic_write_json`` pattern."""
    collection_dir = target_path.parent
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                json.dump(record, tmp_f)
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
    _fsync_dir(collection_dir)


@dataclass
class _SurvivorRecord:
    point_id: str
    path: Path
    project_id: str
    file_hash: str
    old_index: int
    line_start: Optional[int]
    line_end: Optional[int]


def _plan_dedup(
    collection_dir: Path, duplicated: Dict[str, List[Path]]
) -> Dict[str, Optional[Path]]:
    """Pure planning: resolve a winner (or ``None`` -- "no winner, delete
    ALL copies", Story #1560 AC2) per duplicated point_id via
    id_index.bin.

    A value of ``None`` for a point_id means id_index.bin has NO entry
    for it at all, OR its entry resolves to NEITHER scanned copy -- the
    caller deletes every copy symmetrically (AC2) either way. A
    non-None value is the SURVIVING copy -- the caller deletes every
    OTHER copy (AC1).

    Coordinator review finding R1: the "matches neither copy" case was
    ORIGINALLY still raised as ``DuplicateSourceIdError`` on the theory
    that it is a distinct, "more suspicious" anomaly than a simple
    missing entry. Re-examined: it is NOT distinct under AC2's own
    stated rationale ("neither copy is query-visible today"). Every
    file is scanned into ``id_to_paths`` by its OWN content id -- if
    id_index.bin's resolved target genuinely carried this point_id, it
    would already be one of the two scanned copies (AC1's branch). The
    "matches neither" case therefore means id_index.bin's entry for
    this id is itself stale/wrong (points to a path with a different
    id, or a path that no longer exists) -- but critically, NEITHER of
    the two scanned duplicate copies is what index-based hydration
    would serve today, so deleting both changes nothing about what is
    CURRENTLY query-visible for this id. Treating it as a distinct
    raise also reopened Design decision 7's "NO quarantine for this
    cause, unconditionally" through a narrower door:
    ``DuplicateSourceIdError`` is exactly what the scheduler counts
    toward ``consecutive_failure_count``. Folding it into AC2 closes
    that gap. This is NOT the same as AC30's protected case (id_index.bin
    itself failing to LOAD, handled below, unchanged) -- AC30's "only the
    MISSING-entry case" wording scopes to the id_index.bin-load failure,
    not to a stale single-entry mismatch.

    Still raises ``DedupRepairAmbiguousError`` for a corrupt id_index.bin
    itself (AC30, unchanged -- the file fails to LOAD at all, so NO
    entry in it can be trusted, unlike a single stale entry). Performs
    ZERO disk mutation.
    """
    if not duplicated:
        return {}

    try:
        id_index = IDIndexManager().load_index(collection_dir)
    except CorruptIDIndexError as exc:
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: id_index.bin is "
            f"corrupt ({exc}) -- cannot resolve {len(duplicated)} "
            f"duplicate point_id(s) without a trustworthy winner "
            f"reference. Collection left untouched.",
            reason=DedupRepairAmbiguousReason.CORRUPT_ID_INDEX,
        ) from exc

    winners: Dict[str, Optional[Path]] = {}
    for point_id, paths in duplicated.items():
        indexed_path = id_index.get(point_id)
        if indexed_path is None:
            # Story #1560 AC2: no id_index.bin entry at all -- no winner
            # is resolvable. Symmetric: ALL copies will be deleted by the
            # caller. No exception, no arbitrary winner.
            winners[point_id] = None
            continue
        resolved_indexed = indexed_path.resolve()
        matching = [p for p in paths if p.resolve() == resolved_indexed]
        if not matching:
            # R1: a stale/wrong id_index.bin entry -- NEITHER scanned
            # copy is what index-based hydration currently serves for
            # this id. Folded into AC2 (delete all, symmetric) rather
            # than raised: see this function's docstring.
            logger.warning(
                "repair_duplicate_and_shifted_points: %s -- id_index.bin's "
                "entry for duplicate point_id %r (%s) matches NEITHER of "
                "its %d scanned copies; treating as AC2 (no resolvable "
                "winner) -- deleting all copies symmetrically.",
                collection_dir,
                point_id,
                indexed_path,
                len(paths),
            )
            winners[point_id] = None
            continue
        winners[point_id] = matching[0]

    return winners


def _plan_renumber(
    collection_dir: Path,
    survivors: Dict[str, Path],
    identity_by_path: Dict[Path, Dict[str, Any]],
) -> Tuple[Dict[str, Tuple[str, str, int, int]], List[Tuple[str, str, str]]]:
    """Pure planning: parse every survivor record's identity (guaranteed
    present/parseable/self-consistent by the whole-collection identity
    gate, which always runs before this function), group by
    (project_id, file_hash), sort by line order with a real gap-
    continuity check (Claude F1), and compute the canonical renumber
    plan.

    Bug #1502 live-staging amendment: a per-GROUP renumber-safety
    violation (a genuine line gap exceeding the real-chunker-derived
    overlap tolerance, or two distinct records sharing an identical
    (line_start, line_end) range) no longer fails the WHOLE collection.
    Confirmed against a real evolution-repo census: 586 of 10,579 file
    groups (5.5%) carry genuine historical line gaps -- chunks silently
    dropped by the pre-#1502-fix code -- so refusing the whole
    collection over ANY such group would make migration permanently
    impossible for that repo, and realistically every long-lived
    production repo. Instead, that ONE group is EXCLUDED from the
    renumber plan (its records keep their existing point_ids/labels;
    dedup, which is id-keyed and independent of renumbering, still
    applies to it normally) and reported back to the caller as a
    skipped-group entry, for a single summary WARNING. Renumbering
    proceeds normally for every OTHER group. Raises
    DedupRepairAmbiguousError ONLY for the genuinely whole-collection-
    scope anomaly of a MIX of records with and without line_start
    within one group (an internal-invariant-violation-level case,
    unrelated to the two per-group checks above -- left unchanged by
    this amendment). Performs ZERO disk mutation.

    Returns:
        A 2-tuple of:
          - renumber_plan: old point_id -> (new_point_id,
            new_unique_key, new_index, total_chunks), covering every
            record in every group that passed BOTH per-group checks.
          - skipped_groups: (project_id, file_hash, reason) for every
            group excluded from the plan above.
    """
    groups: Dict[Tuple[str, str], List[_SurvivorRecord]] = {}

    for point_id, path in survivors.items():
        record = identity_by_path[path]
        payload = record.get("payload") or {}
        unique_key = payload.get("unique_key")
        try:
            project_id, file_hash, old_index = parse_unique_key(unique_key)
        except ValueError as exc:
            # The whole-collection identity gate (H5/F2) already
            # guarantees every record's unique_key is present, parseable,
            # and self-consistent before this function is ever reached --
            # reaching this branch means the gate itself has a bug, not a
            # legitimate foreign-format record (those are handled by the
            # gate's whole-collection pass-through, never here).
            raise DedupRepairAmbiguousError(
                f"Dedup repair internal invariant violation for "
                f"{collection_dir}: record {path} (point_id {point_id!r}) "
                f"passed the whole-collection identity gate but its "
                f"unique_key ({unique_key!r}) is unparseable here "
                f"({exc}). Collection left untouched; requires manual "
                f"review.",
                reason=DedupRepairAmbiguousReason.INTERNAL_INVARIANT_VIOLATION,
            ) from exc

        groups.setdefault((project_id, file_hash), []).append(
            _SurvivorRecord(
                point_id=point_id,
                path=path,
                project_id=project_id,
                file_hash=file_hash,
                old_index=old_index,
                line_start=payload.get("line_start"),
                line_end=payload.get("line_end"),
            )
        )

    renumber_plan: Dict[str, Tuple[str, str, int, int]] = {}
    skipped_groups: List[Tuple[str, str, str]] = []

    for (project_id, file_hash), items in groups.items():
        has_line_start = [it.line_start is not None for it in items]
        if any(has_line_start) and not all(has_line_start):
            raise DedupRepairAmbiguousError(
                f"Dedup repair refused for {collection_dir}: file group "
                f"{project_id!r}/{file_hash!r} has a mix of records with "
                f"and without line_start -- cannot reliably order them "
                f"for canonical renumbering. Collection left untouched; "
                f"requires manual review.",
                reason=DedupRepairAmbiguousReason.MIXED_LINE_START_PRESENCE,
            )

        if all(has_line_start):
            items.sort(
                key=lambda it: (
                    it.line_start,
                    it.line_end if it.line_end is not None else it.line_start,
                    it.old_index,
                )
            )

            # Bug #1502 live-staging amendment: an identical-line-range
            # ambiguity now excludes ONLY this group from the renumber
            # plan (per-group graceful degradation), never the whole
            # collection.
            seen_ranges = set()
            identical_range_found: Optional[Tuple[Optional[int], Optional[int]]] = None
            for it in items:
                line_range = (it.line_start, it.line_end)
                if line_range in seen_ranges:
                    identical_range_found = line_range
                    break
                seen_ranges.add(line_range)

            if identical_range_found is not None:
                skipped_groups.append(
                    (
                        project_id,
                        file_hash,
                        f"two distinct records share an identical line "
                        f"range {identical_range_found} -- cannot be "
                        f"reliably ordered for canonical renumbering",
                    )
                )
                continue

            # Claude F1: real gap-continuity check, tolerance PROVEN
            # against genuine FixedSizeChunker output (see module-level
            # _GAP_CONTINUITY_SLACK docstring and
            # TestFixedSizeChunkerOverlapDerivesRenumberTolerance). Bug
            # #1502 live-staging amendment: a genuine gap now excludes
            # ONLY this group from the renumber plan, never the whole
            # collection (see this function's docstring).
            gap_reason: Optional[str] = None
            for i in range(len(items) - 1):
                current_item = items[i]
                next_item = items[i + 1]
                # has_line_start (checked above) guarantees both are
                # non-None here -- assert narrows Optional[int] -> int
                # for mypy without weakening the runtime check.
                assert current_item.line_end is not None
                assert next_item.line_start is not None
                if next_item.line_start > current_item.line_end + _GAP_CONTINUITY_SLACK:
                    gap_reason = (
                        f"genuine line gap between old_index="
                        f"{current_item.old_index} (ends line "
                        f"{current_item.line_end}) and old_index="
                        f"{next_item.old_index} (starts line "
                        f"{next_item.line_start}) -- exceeds the real "
                        f"chunker overlap tolerance"
                    )
                    break

            if gap_reason is not None:
                skipped_groups.append((project_id, file_hash, gap_reason))
                continue
        else:
            items.sort(key=lambda it: it.old_index)

        total = len(items)
        for new_index, item in enumerate(items):
            new_unique_key = f"{project_id}_{file_hash}_{new_index}"
            new_point_id = hashlib.md5(new_unique_key.encode()).hexdigest()
            renumber_plan[item.point_id] = (
                new_point_id,
                new_unique_key,
                new_index,
                total,
            )

    return renumber_plan, skipped_groups


def _resolve_hnsw_build_params(collection_dir: Path) -> Tuple[int, str]:
    """Codex LOW finding 5: the SOLE authoritative source for the HNSW
    vector dimension and distance metric is ``collection_meta.json`` --
    NEVER sniffed from vector data, NEVER silently defaulted to a
    hardcoded constant (defaulting to the wrong dimension/metric would
    silently corrupt the rebuilt index). Runs during the PLANNING phase
    (before the marker is ever written), so an undeterminable value
    fails loud, pre-mutation.

    Priority order for the dimension: (1) ``hnsw_index.vector_dim`` (the
    dimension the last successful HNSW build actually used); (2) the
    top-level ``vector_size`` field (written at collection-creation
    time). The distance metric has exactly one authoritative source:
    ``hnsw_index.space``.

    Raises:
        DedupRepairAmbiguousError: collection_meta.json is unreadable/
            malformed, or either value cannot be determined.
    """
    meta_path = collection_dir / _COLLECTION_META_FILENAME
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError) as exc:
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: could not read "
            f"{meta_path} to determine the authoritative HNSW build "
            f"parameters (vector dimension / distance metric) -- "
            f"refusing to guess ({exc}). Collection left untouched.",
            reason=DedupRepairAmbiguousReason.HNSW_PARAMS_META_UNREADABLE,
        ) from exc
    if not isinstance(meta, dict):
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: {meta_path} does "
            f"not contain a JSON object -- cannot determine the "
            f"authoritative HNSW build parameters. Collection left "
            f"untouched.",
            reason=DedupRepairAmbiguousReason.HNSW_PARAMS_META_NOT_OBJECT,
        )

    hnsw_index = meta.get("hnsw_index")
    vector_dim: Optional[int] = None
    space: Optional[str] = None
    if isinstance(hnsw_index, dict):
        candidate_dim = hnsw_index.get("vector_dim")
        if (
            isinstance(candidate_dim, int)
            and not isinstance(candidate_dim, bool)
            and candidate_dim > 0
        ):
            vector_dim = candidate_dim
        candidate_space = hnsw_index.get("space")
        if isinstance(candidate_space, str) and candidate_space in (
            HNSWIndexManager.VALID_SPACES
        ):
            space = candidate_space

    if vector_dim is None:
        candidate_dim = meta.get("vector_size")
        if (
            isinstance(candidate_dim, int)
            and not isinstance(candidate_dim, bool)
            and candidate_dim > 0
        ):
            vector_dim = candidate_dim

    if vector_dim is None:
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: could not "
            f"determine the authoritative vector dimension from "
            f"{meta_path} (neither hnsw_index.vector_dim nor top-level "
            f"vector_size is a valid positive integer) -- refusing to "
            f"guess and rebuild the HNSW index with a potentially WRONG "
            f"dimension. Collection left untouched; requires manual "
            f"review.",
            reason=DedupRepairAmbiguousReason.HNSW_PARAMS_VECTOR_DIM_UNDETERMINABLE,
        )
    if space is None:
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: could not "
            f"determine the authoritative HNSW distance metric from "
            f"{meta_path}'s hnsw_index.space -- refusing to guess "
            f"(defaulting could rebuild with the WRONG metric). "
            f"Collection left untouched; requires manual review.",
            reason=DedupRepairAmbiguousReason.HNSW_PARAMS_SPACE_UNDETERMINABLE,
        )

    return vector_dim, space


def _infer_current_branch(collection_dir: Path) -> Optional[str]:
    """Codex HIGH finding 1: preserve Bug #306's hidden_branches
    branch-isolation semantics across the rebuild by reading the
    collection's OWN previously-recorded ``hnsw_index.current_branch``
    (written by the exact same production mechanism --
    ``HNSWIndexManager._update_metadata``'s ``current_branch`` parameter,
    e.g. via ``FilesystemVectorStore.rebuild_hnsw_filtered``) and
    threading it straight back into ``HNSWIndexManager.
    rebuild_from_vectors(current_branch=...)`` -- REUSING that exact
    existing filtering mechanism, never reimplementing it. Returns None
    (no filtering -- correct for a collection that never recorded a
    branch context) when absent, malformed, or unreadable -- this is
    never a fail-loud condition, since None is the safe, semantically
    correct default for a collection with no recorded branch context.
    """
    meta_path = collection_dir / _COLLECTION_META_FILENAME
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    hnsw_index = meta.get("hnsw_index")
    if not isinstance(hnsw_index, dict):
        return None
    current_branch = hnsw_index.get("current_branch")
    if isinstance(current_branch, str) and current_branch:
        return current_branch
    return None


def _restore_current_branch_in_metadata(
    collection_dir: Path, current_branch: str
) -> None:
    """Codex MEDIUM (round-3 delta review): restore
    ``hnsw_index.current_branch`` in ``collection_meta.json`` after a
    rebuild that genuinely APPLIED hidden_branches filtering via a bare
    ``current_branch`` (``visible_files=None``) call.
    ``HNSWIndexManager._update_metadata`` deliberately persists
    ``current_branch`` ONLY inside its ``filtered`` (visible_files-
    driven) branch -- a separately-tested, intentional design (see
    ``test_hnsw_branch_isolation.py``'s
    ``test_rebuild_from_vectors_current_branch_not_stored_for_
    unfiltered_rebuild``) this module must not disturb for OTHER
    callers of that shared production function. Without this restore, a
    LATER rebuild (e.g. a query-time staleness rebuild) reading only the
    persisted metadata would see no branch context and incorrectly
    re-include the previously-hidden vectors. Scoped entirely to the
    repair path; reuses the SAME atomic+durable metadata writer (Bug
    #1407 pattern) already used elsewhere in this codebase.
    ``collection_meta.json`` itself is guaranteed present at this call
    site -- the immediately-preceding ``HNSWIndexManager.
    rebuild_from_vectors`` call already requires it and would itself
    raise ``FileNotFoundError`` first if it were missing. This function
    is a no-op only when the ``hnsw_index`` KEY is absent/malformed (the
    rebuild published no metadata section at all -- a separate,
    pre-existing ``rebuild_from_vectors`` behavior, out of scope for
    this fix) or when the value is already correct."""
    meta_path = collection_dir / _COLLECTION_META_FILENAME
    with open(meta_path) as f:
        meta = json.load(f)
    hnsw_index = meta.get("hnsw_index")
    if not isinstance(hnsw_index, dict):
        return
    if hnsw_index.get("current_branch") == current_branch:
        return
    hnsw_index["current_branch"] = current_branch
    HNSWIndexManager._atomic_write_metadata_durable(collection_dir, meta)


def _rebuild_derived_artifacts(
    collection_dir: Path, vector_dim: int, space: str
) -> None:
    """Rebuild id_index.bin and the HNSW index (+ id_mapping) from the
    CURRENT, repaired JSON tree -- the single source of truth for
    correctness after a dedup/renumber pass. Reuses existing, already-
    battle-tested machinery (never a bespoke forward-map): quarantined
    losers are physically absent from the tree, so they cannot re-appear
    as a duplicate HNSW label for the same id (closes Codex H3). Threads
    the collection's recorded current_branch through so hidden_branches
    branch-isolation semantics (Bug #306) survive the rebuild unchanged
    (closes Codex HIGH finding 1), and durably restores that same branch
    context in collection_meta.json afterward so it survives for a LATER
    rebuild too (closes Codex MEDIUM round-3 finding).

    Bug #1969 F2: self_heal=False is EXPLICIT here -- a duplicate found by
    THIS internal rebuild must never trigger a SECOND repair_duplicate_
    and_shifted_points() call recursively; it must raise
    DuplicateSourceIdError immediately, exactly as it did before Bug
    #1969's self-heal existed."""
    IDIndexManager().rebuild_from_vectors(collection_dir, self_heal=False)

    current_branch = _infer_current_branch(collection_dir)
    HNSWIndexManager(vector_dim=vector_dim, space=space).rebuild_from_vectors(
        collection_dir,
        current_branch=current_branch,
        layout_override=ChunkLayout.SHARDED_JSON,
    )

    if current_branch is not None:
        _restore_current_branch_in_metadata(collection_dir, current_branch)


def repair_duplicate_and_shifted_points(
    collection_dir: "Path | str",
    *,
    deletion_authorized: bool = True,
    query_tracker: Optional[Any] = None,
    refcount_key: Optional[str] = None,
    drain_max_wait_seconds: Optional[float] = None,
) -> DedupRepairResult:
    """Metadata-only dedup + canonical renumber repair for ONE legacy
    SHARDED_JSON collection directory.

    Args:
        deletion_authorized: Story #1560 AC19 / Story #1460's rollout-
            safety gate. Defaults to True (byte-identical for every
            pre-existing caller). When False AND duplicate point_id
            groups are detected, this call performs ZERO mutation --
            no deletion, no renumbering, no marker, no rebuild -- and
            returns a result with ``deletion_gated=True``. The caller
            (``consolidate_collection_in_place``) must NOT report a
            completed dedup outcome in that case.
        query_tracker: Story #1560 AC20/AC21. When given together with
            ``refcount_key``, and duplicate groups are about to be
            deleted, the key is marked quiescing (refusing new query
            admissions) and drained with a bounded wait -- reusing
            Story #1458 AC13's ``wait_for_activated_repo_query_drain``
            -- before any file is deleted, and the mark is cleared on
            EVERY exit path (success or exception). None (the default)
            is a fail-open no-op, matching every pre-existing caller.
        refcount_key: The query-tracker key for this collection's owning
            repository (see ``query_tracker``).
        drain_max_wait_seconds: Optional override for the bounded drain
            wait, threaded through for test determinism. None resolves
            the live config default (production behavior).

    See module docstring for the full algorithm and the rebuild-derived-
    artifacts crash-convergence mechanism.
    """
    collection_dir = Path(collection_dir)
    marker_exists_on_entry = _marker_path(collection_dir).exists()

    # ---- PHASE 1: pure planning -- zero disk mutation ----
    id_to_paths, malformed, identity_by_path = _scan_raw_records(collection_dir)

    if malformed:
        sample = malformed[:_MAX_MALFORMED_SAMPLE_SIZE]
        raise DedupRepairAmbiguousError(
            f"Dedup repair refused for {collection_dir}: "
            f"{len(malformed)} malformed vector record(s) found "
            f"(sample: {[(str(p), reason) for p, reason in sample]}) -- "
            f"refusing to mutate ANY record in this collection before "
            f"the malformed one(s) can be reviewed. Collection left "
            f"untouched.",
            reason=DedupRepairAmbiguousReason.MALFORMED_RECORDS,
        )

    if not id_to_paths:
        if marker_exists_on_entry:
            # Codex HIGH finding 2 (delta re-review): a stale marker with
            # a now-EMPTY tree is anomalous -- fail loud instead of
            # silently "converging" (which would call
            # HNSWIndexManager.rebuild_from_vectors against zero files,
            # returning early WITHOUT touching a pre-existing stale
            # hnsw_index.bin, leaving stale vectors falsely queryable
            # forever). The marker is deliberately NEVER touched here --
            # crash evidence for manual review stays intact.
            raise DedupRepairAmbiguousError(
                f"Dedup repair refused for {collection_dir}: a prior "
                f"repair pass's crash marker ({_MARKER_FILENAME}) is "
                f"present but the collection now has ZERO vector "
                f"records -- this is anomalous (a genuinely-always-"
                f"empty collection should never carry this marker; data "
                f"may have been unexpectedly lost mid-repair). Refusing "
                f"to silently converge over a potentially stale HNSW "
                f"index. Collection left untouched (marker NOT "
                f"deleted); requires manual review.",
                reason=DedupRepairAmbiguousReason.STALE_MARKER_EMPTY_TREE,
            )
        return DedupRepairResult()

    if not _whole_collection_identity_gate_passes(id_to_paths, identity_by_path):
        logger.info(
            "repair_duplicate_and_shifted_points: %s does not use this "
            "repair's identity scheme (a missing, foreign, or non-self-"
            "consistent unique_key was found on at least one record) -- "
            "passing the WHOLE collection through untouched; migration "
            "proceeds exactly as it did before this repair existed (its "
            "own scan still quarantines any genuine duplicate point_id "
            "as before).",
            collection_dir,
        )
        return DedupRepairResult(
            records_scanned=len(id_to_paths),
            gate_passed=False,
            duplicate_groups=sum(1 for paths in id_to_paths.values() if len(paths) > 1),
        )

    duplicated = {pid: paths for pid, paths in id_to_paths.items() if len(paths) > 1}
    collection_total = sum(len(paths) for paths in id_to_paths.values())

    # ---- Story #1560 AC19: deletion_authorized=False gate ----
    if duplicated and not deletion_authorized:
        logger.warning(
            "repair_duplicate_and_shifted_points: %d duplicate point_id "
            "group(s) detected in %s but deletion_authorized=False "
            "(Story #1460 rollout gate closed) -- performing ZERO "
            "mutation and reporting deletion_gated=True; this collection "
            "cannot be safely consolidated until the gate opens.",
            len(duplicated),
            collection_dir,
        )
        return DedupRepairResult(
            records_scanned=len(id_to_paths),
            gate_passed=True,
            duplicate_groups=len(duplicated),
            records_before=collection_total,
            collection_total=collection_total,
            deletion_gated=True,
        )

    # May raise DedupRepairAmbiguousError (corrupt id_index.bin) --
    # collection left untouched (AC30). A stale/wrong single entry
    # (matches neither copy) no longer raises; it is folded into AC2's
    # delete-all-copies branch (coordinator review finding R1).
    winners = _plan_dedup(collection_dir, duplicated)

    survivors: Dict[str, Path] = {}
    winner_kept_groups = 0
    whole_group_deleted_groups = 0
    records_deleted = 0
    for point_id, paths in id_to_paths.items():
        if point_id not in duplicated:
            # Bug #1747: a hidden_branches-only bookkeeping sentinel
            # passed the whole-collection identity gate above (it was
            # deliberately SKIPPED there, not identity-checked) -- it
            # carries no unique_key/line info to renumber by, so it must
            # never become a _plan_renumber survivor (which requires a
            # parseable unique_key for every entry). Left untouched.
            if _is_hidden_branches_only_bookkeeping_record(identity_by_path[paths[0]]):
                continue
            survivors[point_id] = paths[0]
            continue
        winner_path = winners[point_id]
        if winner_path is None:
            # Story #1560 AC2: no resolvable winner -- ALL copies of
            # this group are deleted below; no survivor for this id.
            whole_group_deleted_groups += 1
            records_deleted += len(paths)
            continue
        # Story #1560 AC1: winner resolved -- every OTHER copy deleted.
        winner_kept_groups += 1
        records_deleted += len(paths) - 1
        # Bug #1747 (defense-in-depth): same exclusion as above, for the
        # edge case of a duplicated point_id whose winner copy is itself
        # a bookkeeping sentinel -- deletion of losers below is
        # independent of `survivors` and proceeds normally either way.
        if _is_hidden_branches_only_bookkeeping_record(identity_by_path[winner_path]):
            continue
        survivors[point_id] = winner_path

    renumber_plan, skipped_groups = _plan_renumber(
        collection_dir, survivors, identity_by_path
    )

    skipped_sample = [
        file_hash for _, file_hash, _ in skipped_groups[:_MAX_SKIPPED_GROUP_SAMPLE_SIZE]
    ]
    if skipped_groups:
        # Bug #1502 live-staging amendment: ONE summary WARNING for every
        # group excluded from renumbering this call -- their labels
        # remain non-canonical, but the collection as a whole still
        # repairs and consolidates normally.
        logger.warning(
            "repair_duplicate_and_shifted_points: %d file group(s) in %s "
            "skipped renumbering due to a genuine line gap or an "
            "identical-line-range ambiguity (sample file_hash(es): %s) -- "
            "their labels remain non-canonical; consequence: those files "
            "will re-identify (receive new point_ids) on their next real "
            "re-index, handled by the existing per-file orphan cleanup "
            "mechanism.",
            len(skipped_groups),
            collection_dir,
            skipped_sample,
        )

    records_would_renumber = sum(
        1 for pid, plan in renumber_plan.items() if plan[0] != pid
    )
    anything_changed = bool(duplicated) or records_would_renumber > 0
    rebuild_required = marker_exists_on_entry or anything_changed

    if not rebuild_required:
        # Identity fast path: nothing to do, no marker existed -- zero
        # mutation, zero rebuild cost for an already-clean collection.
        return DedupRepairResult(
            records_scanned=len(id_to_paths),
            gate_passed=True,
            groups_skipped_renumber=len(skipped_groups),
            skipped_renumber_file_hashes=skipped_sample,
        )

    # Codex LOW finding 5: resolve the authoritative HNSW build
    # parameters BEFORE the marker is written -- an undeterminable value
    # fails loud, pre-mutation, never silently defaulted.
    vector_dim, space = _resolve_hnsw_build_params(collection_dir)

    # ---- Story #1560 AC20/AC21: quiesce query-tracker key BEFORE any
    # deletion, and clear the mark on EVERY exit path. Only relevant
    # when there is something to delete AND a tracker/key were supplied
    # -- otherwise a true no-op, matching every pre-existing caller.
    quiescing_marked = (
        bool(duplicated) and query_tracker is not None and refcount_key is not None
    )
    if quiescing_marked and query_tracker is not None:
        query_tracker.mark_quiescing(refcount_key)
    try:
        if quiescing_marked:
            # Lazy import: this module is imported by the CLI/solo path
            # (storage/shared/), which must never eagerly pull in the
            # server package (Bug #1468's lazy-load discipline) -- only
            # a caller that actually supplied a query_tracker (a server
            # caller) pays this import cost.
            from code_indexer.server.services.deactivation_query_drain import (
                wait_for_activated_repo_query_drain,
            )

            wait_for_activated_repo_query_drain(
                query_tracker,
                refcount_key,
                max_wait_seconds=drain_max_wait_seconds,
            )

        # ---- Story #1560 AC22: crash-durable outcome journal, written
        # BEFORE any deletion -- see _accumulate_pending_outcome_durably
        # for why this is a distinct file from the marker below (its
        # lifecycle is owned by the upstream DB-persistence layer, not
        # by this function). Only when there is a dedup outcome worth
        # journaling this pass (AC10: a clean renumber-only pass writes
        # nothing here). records_before/collection_total both source
        # from the SAME `collection_total` local -- they are the same
        # measured quantity under two names (see DedupRepairResult's
        # own docstring and this function's final return below).
        if duplicated:
            _accumulate_pending_outcome_durably(
                collection_dir,
                {
                    "duplicate_groups": len(duplicated),
                    "records_deleted": records_deleted,
                    "winner_kept_groups": winner_kept_groups,
                    "whole_group_deleted_groups": whole_group_deleted_groups,
                    "records_before": collection_total,
                    "collection_total": collection_total,
                },
            )

        # ---- Marker written durably BEFORE the first mutation ----
        _write_marker_durably(collection_dir)

        # ---- PHASE 2: apply -- DELETE outright, never quarantine ----
        for point_id, paths in duplicated.items():
            winner_path = winners[point_id]
            if winner_path is None:
                # AC2: no winner -- delete ALL copies symmetrically.
                for candidate in paths:
                    _delete_loser(candidate)
            else:
                # AC1: delete every copy that is NOT the winner.
                for candidate in paths:
                    if candidate.resolve() != winner_path.resolve():
                        _delete_loser(candidate)

        records_renumbered = 0
        for old_point_id, path in survivors.items():
            if old_point_id not in renumber_plan:
                # Bug #1502 live-staging amendment: this record belongs
                # to a group excluded from the renumber plan (genuine
                # line gap or identical-line-range ambiguity) -- left
                # byte-identical.
                continue
            new_point_id, new_unique_key, new_index, total = renumber_plan[old_point_id]
            # Bug #1558: re-read the FULL record fresh from disk at
            # write time rather than reusing a value retained since the
            # planning scan -- the planning phase only keeps a
            # lightweight identity view (see
            # _extract_lightweight_identity_fields), never the full
            # record (which includes the embedding vector and can be
            # tens of KB per record). Safe: `path` is a survivor's
            # ORIGINAL, undeleted file -- the dedup-deletion loop above
            # only deletes LOSER paths, never a survivor's own path --
            # so its on-disk content here is exactly what the scan
            # parsed.
            record = _read_full_json_record(path)
            record["id"] = new_point_id
            payload = record.setdefault("payload", {})
            payload["point_id"] = new_point_id
            payload["unique_key"] = new_unique_key
            payload["chunk_index"] = new_index
            payload["total_chunks"] = total
            if new_point_id != old_point_id:
                records_renumbered += 1
            _atomic_write_json_record(path, record)

        # ---- PHASE 3: rebuild id_index.bin + HNSW from repaired truth
        # ---- (skipped groups' unchanged records are naturally
        # included -- the rebuild scans the CURRENT JSON tree, which
        # still contains them under their existing point_ids.)
        _rebuild_derived_artifacts(collection_dir, vector_dim, space)

        # ---- Codex review Finding F1: only NOW, with the deletion loop
        # and BOTH rebuilds proven complete, may the outcome journal (if
        # any -- absent for a renumber-only pass) be flipped to
        # "completed". Unconditional: covers both this pass having just
        # written a fresh "pending" journal above, AND the marker-
        # driven convergence case (duplicated empty this pass, but a
        # stale "pending" journal from an EARLIER crashed pass survives
        # -- reaching this line proves that earlier pass's deletion
        # actually completed for real). ----
        _mark_pending_outcome_completed_durably(collection_dir)

        # ---- Marker deleted only after BOTH rebuilds complete ----
        _delete_marker_durably(collection_dir)
    finally:
        if quiescing_marked and query_tracker is not None:
            query_tracker.clear_quiescing(refcount_key)

    return DedupRepairResult(
        records_scanned=len(id_to_paths),
        duplicate_groups=len(duplicated),
        records_before=collection_total,
        records_deleted=records_deleted,
        winner_kept_groups=winner_kept_groups,
        whole_group_deleted_groups=whole_group_deleted_groups,
        collection_total=collection_total,
        records_renumbered=records_renumbered,
        gate_passed=True,
        id_index_rebuilt=True,
        hnsw_rebuilt=True,
        groups_skipped_renumber=len(skipped_groups),
        skipped_renumber_file_hashes=skipped_sample,
    )


@dataclass
class DuplicateFileWipeResult:
    """Outcome of one :func:`recover_from_corrupt_id_index_by_wiping_files`
    call."""

    #: Number of distinct (project_id, file_hash) files wholly wiped.
    file_hashes_wiped: int = 0
    #: Total physical vector_*.json records deleted across all wiped
    #: files (every chunk of each implicated file, not just the
    #: colliding ones).
    records_deleted: int = 0
    #: Bug #1969 Round 4 (R3-F1): the relative path (``payload.path``) of
    #: every wiped file, so a caller (IDIndexManager.rebuild_from_
    #: vectors(), and ultimately smart_indexer.py's incremental indexing
    #: loop) can re-queue these files for reprocessing within the SAME
    #: run instead of leaving them permanently unsearchable until an
    #: unrelated edit or a manual --reconcile.
    wiped_relative_paths: FrozenSet[str] = field(default_factory=frozenset)


def _try_parse_record_file_identity(
    record: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """Best-effort (project_id, file_hash) extraction from one raw
    lightweight identity record's payload.unique_key -- returns None
    (never raises) for anything unparseable, so callers can uniformly
    skip records this recovery cannot safely classify."""
    payload = record.get("payload") or {}
    unique_key = payload.get("unique_key")
    try:
        project_id, file_hash, _old_index = parse_unique_key(unique_key)
    except ValueError:
        return None
    return project_id, file_hash


def _is_project_confined_relative_path(rel_path: Any) -> bool:
    """Bug #1969 Round 6 (P1-5): true when `rel_path` is a nonempty
    string that is safely usable as a project-relative replay path --
    NOT absolute, and does not escape its base via a `..` path-traversal
    segment. Validated syntactically against a synthetic root (this
    module has no access to the real codebase root -- it only ever sees
    the vector store's own collection directory), so this proves the
    path COULD be a valid relative path within a project, without
    needing the actual filesystem tree.
    """
    if not isinstance(rel_path, str) or not rel_path:
        return False
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return False
    synthetic_root = Path("/__self_heal_replay_path_validation_root__")
    resolved = (synthetic_root / candidate).resolve()
    try:
        resolved.relative_to(synthetic_root)
    except ValueError:
        return False
    return True


def recover_from_corrupt_id_index_by_wiping_files(
    collection_dir: "Path | str",
) -> DuplicateFileWipeResult:
    """Bug #1969 Round 3: whole-file-wipe escalation for the ONE
    DedupRepairAmbiguousError reason that is safe to auto-resolve --
    ``DedupRepairAmbiguousReason.CORRUPT_ID_INDEX``. Called ONLY by
    ``IDIndexManager.rebuild_from_vectors``'s self-heal escalation, and
    ONLY after :func:`repair_duplicate_and_shifted_points` has already
    raised that specific reason: ``id_index.bin`` itself failed to load,
    so ``_plan_dedup`` has no trustworthy reference to pick a per-chunk
    winner among a duplicate point_id's colliding copies.

    Recovery here does NOT depend on ``id_index.bin`` at all: for every
    duplicate point_id group, every conflicting record's ``unique_key``
    is parsed to its ``(project_id, file_hash)`` identity -- a point_id
    collision can ONLY occur between chunks of the SAME source file
    (identical project_id + file_hash + chunk_index), so recovery is
    naturally bounded to those implicated files. Because this function
    is only reached after ``repair_duplicate_and_shifted_points`` has
    already run its whole-collection identity gate and malformed-record
    pre-check, every duplicate-group record here is guaranteed to carry
    a parseable, self-consistent ``unique_key``.

    EVERY vector_*.json record belonging to any implicated
    ``(project_id, file_hash)`` -- not just the colliding chunks -- is
    deleted, so the file ends up with ZERO indexed chunks rather than a
    partial/inconsistent set. This is deliberate: smart_indexer.py's
    reconcile decides whether a file needs reprocessing using ONLY a
    file-level identity comparison (git blob hash / mtime) -- it has no
    notion of "does this file have all its expected chunks present". A
    half-deleted file with an unchanged blob hash would never be
    reprocessed again (a silent, permanent search-coverage gap). A
    WHOLLY missing file is instead correctly detected as "needs
    reprocessing" and fully re-indexed on the next real run.

    Metadata-only: no re-chunking, no re-embedding, no provider calls.
    Each deletion fsyncs its containing directory (reuses
    :func:`_delete_loser`). ``id_index.bin`` and the HNSW index are then
    rebuilt from the remaining, now conflict-free records via the SAME
    already-battle-tested :func:`_rebuild_derived_artifacts` machinery
    this module's normal repair uses -- its internal
    ``IDIndexManager.rebuild_from_vectors`` call already passes
    ``self_heal=False`` explicitly (Round 2's F2 fix), preventing this
    escalation from ever recursing into itself.

    Applies the SAME ``.versioned/`` snapshot guard as the rest of this
    project (defense-in-depth: the caller already checks this before
    ever reaching here, but this function must be safe to call
    standalone too) -- raises ``DuplicateSourceIdError`` for that case,
    matching that exception's own documented "an immutable versioned
    snapshot" cause.

    Returns:
        A :class:`DuplicateFileWipeResult` describing what was wiped.
        Zero-valued (no mutation) when the collection currently has no
        duplicate point_id at all.

    Raises:
        DuplicateSourceIdError: ``collection_dir`` is at or inside an
            immutable ``.versioned/`` snapshot.
        DedupRepairAmbiguousError: a malformed record is found during
            this function's own re-scan (defensive; should not happen
            given the caller's own pre-checks already passed), or the
            authoritative HNSW build parameters cannot be determined --
            both propagate unchanged, exactly like the normal repair
            path.
    """
    collection_dir = Path(collection_dir)

    from code_indexer.server.services.query_path_cache import (
        is_immutable_versioned_snapshot,
    )

    if is_immutable_versioned_snapshot(str(collection_dir)):
        logger.warning(
            "Bug #1969 Round 3: recover_from_corrupt_id_index_by_wiping_"
            "files refused for %r -- immutable versioned snapshot; "
            "refusing to mutate regardless of what was requested.",
            collection_dir,
        )
        raise DuplicateSourceIdError(
            f"recover_from_corrupt_id_index_by_wiping_files refused for "
            f"{collection_dir}: path is an immutable versioned snapshot "
            f"-- refusing to mutate a snapshot regardless of what was "
            f"requested."
        )

    id_to_paths, malformed, identity_by_path = _scan_raw_records(collection_dir)

    if malformed:
        sample = malformed[:_MAX_MALFORMED_SAMPLE_SIZE]
        raise DedupRepairAmbiguousError(
            f"recover_from_corrupt_id_index_by_wiping_files refused for "
            f"{collection_dir}: {len(malformed)} malformed vector "
            f"record(s) found (sample: "
            f"{[(str(p), r) for p, r in sample]}) -- refusing to mutate "
            f"ANY record before the malformed one(s) can be reviewed.",
            reason=DedupRepairAmbiguousReason.MALFORMED_RECORDS,
        )

    duplicated = {
        point_id: paths for point_id, paths in id_to_paths.items() if len(paths) > 1
    }
    if not duplicated:
        return DuplicateFileWipeResult()

    implicated_files = set()
    for paths in duplicated.values():
        for path in paths:
            identity = _try_parse_record_file_identity(identity_by_path[path])
            if identity is not None:
                implicated_files.add(identity)

    if not implicated_files:
        # No duplicate group had a parseable unique_key -- this recovery
        # cannot safely identify which files to wipe. Should not happen
        # in practice (the caller only reaches here after the whole-
        # collection identity gate already passed), but a genuine no-op
        # is safer than guessing.
        return DuplicateFileWipeResult()

    # Codex LOW finding 5 pattern (mirrors repair_duplicate_and_shifted_
    # points): resolve the authoritative HNSW build parameters BEFORE any
    # mutation -- an undeterminable value must fail loud, pre-mutation,
    # leaving the collection untouched, never after files have already
    # been deleted.
    vector_dim, space = _resolve_hnsw_build_params(collection_dir)

    # Pure planning pass: compute which relative paths will be wiped, PER
    # implicated identity -- NO deletion happens here.
    valid_paths_by_identity: Dict[Tuple[str, str], set] = {}
    for path, record in identity_by_path.items():
        identity = _try_parse_record_file_identity(record)
        if identity is not None and identity in implicated_files:
            payload = record.get("payload") or {}
            rel_path = payload.get("path")
            if _is_project_confined_relative_path(rel_path):
                valid_paths_by_identity.setdefault(identity, set()).add(rel_path)

    # Bug #1969 Round 6 (P1-5): refuse -- BEFORE recording the sidecar or
    # deleting anything -- if ANY implicated identity has NO record with
    # a valid, project-confined replay path (missing, empty, absolute, or
    # a `..` path-traversal escape). Deleting such a group's records
    # would lose the only durably nameable replay path with zero
    # mutation to show for it -- unlike a genuinely malformed record
    # (caught earlier by the whole-collection scan), this shape is
    # syntactically valid JSON with a parseable identity, so it slips
    # past every OTHER precheck in this module.
    unresolvable_identities = implicated_files - set(valid_paths_by_identity.keys())
    if unresolvable_identities:
        raise DedupRepairAmbiguousError(
            f"recover_from_corrupt_id_index_by_wiping_files refused for "
            f"{collection_dir}: {len(unresolvable_identities)} implicated "
            f"file-hash group(s) have NO record with a valid, "
            f"project-confined replay path (sample identities: "
            f"{sorted(unresolvable_identities)[:_MAX_MALFORMED_SAMPLE_SIZE]}) "
            f"-- refusing to delete records that cannot be durably "
            f"recorded for reprocessing.",
            reason=DedupRepairAmbiguousReason.UNRESOLVABLE_REPLAY_PATH,
        )

    wiped_relative_paths: set = set()
    for paths in valid_paths_by_identity.values():
        wiped_relative_paths |= paths

    # Bug #1969 Round 5 (R4-F1): durably record the about-to-be-wiped
    # paths BEFORE the first physical deletion below -- a crash anywhere
    # after this point (mid-deletion, or during the HNSW rebuild's long
    # lock hold, R3-F5) still leaves a durable record for the NEXT run's
    # own consult-and-fold-in step to pick up, so the file is never
    # silently, permanently lost regardless of which caller (upsert_
    # points, end_indexing, scroll_points, in this run or a future one)
    # triggered this wipe.
    record_self_heal_reprocess_pending(collection_dir, frozenset(wiped_relative_paths))

    records_deleted = 0
    for path, record in identity_by_path.items():
        identity = _try_parse_record_file_identity(record)
        if identity is not None and identity in implicated_files:
            _delete_loser(path)
            records_deleted += 1

    logger.warning(
        "Bug #1969 Round 4: recover_from_corrupt_id_index_by_wiping_files "
        "wiped %d record(s) across %d implicated file(s) in %s -- "
        "id_index.bin was corrupt/unreadable so no per-chunk winner "
        "could be resolved; every chunk of each implicated file was "
        "deleted (not just the colliding ones) so the file is fully "
        "MISSING rather than half-indexed. Wiped relative paths: %s.",
        records_deleted,
        len(implicated_files),
        collection_dir,
        sorted(wiped_relative_paths),
    )

    _rebuild_derived_artifacts(collection_dir, vector_dim, space)

    return DuplicateFileWipeResult(
        file_hashes_wiped=len(implicated_files),
        records_deleted=records_deleted,
        wiped_relative_paths=frozenset(wiped_relative_paths),
    )
