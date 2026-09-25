"""ID index manager for fast point_id to file_path mapping.

Maintains a persistent binary file mapping vector IDs to their file paths
using mmap for fast loading and minimal memory overhead.
"""

import logging
import os
import struct
from pathlib import Path
from typing import Dict, List, Tuple
import threading

from code_indexer.services.temporal.temporal_structure_marker import (
    STRUCTURE_MARKER_FILENAME,
)
from code_indexer.storage.shared.hnsw_sync_state import HNSW_SYNC_STATE_FILENAME
from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)

_MAX_INDEX_ENTRIES = 10_000_000
_HEADER_SIZE = 4  # bytes occupied by the uint32 entry-count field

# Bug #1297: Story #1290 per-commit temporal indexing writes bookkeeping /
# marker JSON sidecars alongside vector JSON files in a collection dir. These
# files structurally lack an 'id' field by design (they are not vectors), so
# they must be skipped WITHOUT the "missing 'id' field" WARNING that a
# genuinely malformed vector file would (correctly) still trigger.
#
# temporal_structure.json has an exported constant (imported above);
# temporal_progress.json / temporal_meta.json are referenced as literal
# strings elsewhere in the temporal package (temporal_collection_naming.py,
# temporal_migration.py) with no shared constant to import.
# Story #1458 Codex CRITICAL finding (round 4): collection_migration.py's
# crash-durable content-integrity manifest is migration-engine bookkeeping,
# not a vector record -- skipped by name, same convention as the entries
# below (a bare string literal here, no import, matching how
# temporal_progress.json/temporal_meta.json are referenced).
_CHUNKS_DB_CONTENT_MANIFEST_FILENAME = "chunks_db_content_manifest.json"

_TEMPORAL_BOOKKEEPING_FILENAMES = frozenset(
    {
        STRUCTURE_MARKER_FILENAME,  # temporal_structure.json
        "temporal_progress.json",
        "temporal_meta.json",
    }
)


class CorruptIDIndexError(Exception):
    """Raised when id_index.bin is detected to be corrupt or truncated.

    Callers that catch this error may trigger rebuild_from_vectors() to
    auto-repair the index from the intact vector JSON files on disk.
    """


class DuplicateSourceIdError(Exception):
    """Raised when two or more distinct source vector files share the
    same point_id (Story #1458 round-6 Codex CRITICAL finding #5).

    A primary-key store (chunks.db) cannot resolve this ambiguity --
    silently picking one file as "the winner" would discard the other
    file's data, and downstream cleanup would then delete BOTH source
    files, permanently losing whichever record lost the silent race.
    It must never be SILENTLY auto-resolved by simply picking a winner.

    Bug #1969: IDIndexManager.rebuild_from_vectors(self_heal=True) (used
    by genuine write-path callers: cidx index / upsert / end-of-indexing /
    temporal reconciliation) attempts a one-shot metadata-only repair
    (repair_duplicate_and_shifted_points()) before re-raising this error --
    so by the time this exception actually reaches a caller, it means
    EITHER (a) self_heal was not requested at all (every read/query-path
    caller, which must never attempt to mutate the collection), or (b)
    the one-shot self-heal already ran and could NOT resolve the
    ambiguity (e.g. a foreign/missing identity scheme the repair's
    whole-collection identity gate refuses to touch, or an immutable
    versioned snapshot). In either case, explicit operator intervention
    (or the fleet-migration consolidation path) is the only remaining
    recovery to determine which file is correct (or whether both need to
    be re-indexed under distinct ids).
    """


class IDIndexManager:
    """Manages persistent ID index for fast lookups using binary format.

    Binary Format Specification:
    [num_entries: 4 bytes (uint32, little-endian)]
    For each entry:
      [id_length: 2 bytes (uint16, little-endian)]
      [id_string: variable UTF-8 bytes]
      [path_length: 2 bytes (uint16, little-endian)]
      [path_string: variable UTF-8 bytes, relative to collection]
    """

    INDEX_FILENAME = "id_index.bin"

    def __init__(self):
        """Initialize IDIndexManager."""
        self._lock = threading.RLock()  # Reentrant lock to allow nested locking

    @staticmethod
    def _read_exact(f, size: int, context: str) -> bytes:
        """Read exactly `size` bytes or raise CorruptIDIndexError."""
        data = bytes(f.read(size))
        if len(data) < size:
            raise CorruptIDIndexError(f"id_index.bin truncated: EOF reading {context}")
        return data

    @staticmethod
    def _read_u16(f, context: str) -> int:
        """Read a little-endian uint16 or raise CorruptIDIndexError."""
        return int(struct.unpack("<H", IDIndexManager._read_exact(f, 2, context))[0])

    @staticmethod
    def _read_u32(f, context: str) -> int:
        """Read a little-endian uint32 or raise CorruptIDIndexError."""
        return int(struct.unpack("<I", IDIndexManager._read_exact(f, 4, context))[0])

    @staticmethod
    def _read_utf8_string(f, length: int, context: str) -> str:
        """Read `length` UTF-8 bytes and decode them or raise CorruptIDIndexError."""
        raw = IDIndexManager._read_exact(f, length, context)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: invalid UTF-8 in {context}"
            ) from exc

    @staticmethod
    def _safe_relative_path(path_str: str, context: str) -> Path:
        """Validate that path_str is a safe relative path.

        Rejects absolute paths and paths that escape the collection directory
        via ``..`` traversal, raising CorruptIDIndexError on invalid input.
        """
        p = Path(path_str)
        if p.is_absolute():
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} is an absolute path: {path_str!r}"
            )
        # Normalise and check for escaping parent components
        try:
            normalised = Path(*p.parts)  # reconstructs without redundant separators
        except Exception:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} is not a valid path: {path_str!r}"
            )
        if ".." in normalised.parts:
            raise CorruptIDIndexError(
                f"id_index.bin corrupt: {context} escapes collection directory: {path_str!r}"
            )
        return normalised

    def load_index(self, collection_path: Path) -> Dict[str, Path]:
        """Load ID index from disk.

        Returns:
            Dictionary mapping point IDs to absolute file paths

        Raises:
            CorruptIDIndexError: File is zero bytes, too small for the header,
                has an unreasonable entry count, or is truncated mid-entry.
                Callers should catch this and call rebuild_from_vectors().
        """
        index_file = collection_path / self.INDEX_FILENAME
        if not index_file.exists():
            return {}

        with open(index_file, "rb") as f:
            file_size = f.seek(0, 2)
            f.seek(0)

            if file_size == 0:
                raise CorruptIDIndexError(
                    "id_index.bin is zero bytes (interrupted write)"
                )
            if file_size < _HEADER_SIZE:
                raise CorruptIDIndexError(
                    f"id_index.bin too small for entry-count header ({file_size} bytes)"
                )

            num_entries = self._read_u32(f, "entry-count header")
            if num_entries > _MAX_INDEX_ENTRIES:
                raise CorruptIDIndexError(
                    f"id_index.bin has unreasonable entry count: {num_entries} "
                    f"(max {_MAX_INDEX_ENTRIES})"
                )

            id_index: Dict[str, Path] = {}
            for _ in range(num_entries):
                id_len = self._read_u16(f, "ID length")
                point_id = self._read_utf8_string(f, id_len, "ID string")
                path_len = self._read_u16(f, "path length")
                path_str = self._read_utf8_string(f, path_len, "path string")
                safe_path = self._safe_relative_path(path_str, "path string")
                id_index[point_id] = collection_path / safe_path

            return id_index

    def save_index(self, collection_path: Path, id_index: Dict[str, Path]) -> None:
        """Save ID index to disk atomically (temp-file + os.replace).

        Bug #1575 round 6 item 3b: per-call-unique temp filename (pid +
        thread-id) -- a fixed name raced across the fresh
        ``IDIndexManager()`` instance every call site constructs.
        """
        index_file = collection_path / self.INDEX_FILENAME
        temp_file = index_file.with_name(
            f"{index_file.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
        )

        with self._lock:
            try:
                with open(temp_file, "wb") as f:
                    f.write(struct.pack("<I", len(id_index)))
                    for point_id, file_path in id_index.items():
                        try:
                            relative_path = file_path.relative_to(collection_path)
                            path_str = str(relative_path)
                        except ValueError:
                            path_str = str(file_path)
                        id_bytes = point_id.encode("utf-8")
                        path_bytes = path_str.encode("utf-8")
                        f.write(struct.pack("<H", len(id_bytes)))
                        f.write(id_bytes)
                        f.write(struct.pack("<H", len(path_bytes)))
                        f.write(path_bytes)
                    f.flush()
                    nfs_safe_fsync(f.fileno())
                os.replace(temp_file, index_file)
            except Exception:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError as cleanup_err:
                    logger.warning("Cleanup failed for %s: %s", temp_file, cleanup_err)
                raise

            dir_fd = os.open(str(collection_path), os.O_RDONLY)
            try:
                nfs_safe_fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def update_batch(self, collection_path: Path, updates: Dict[str, Path]) -> None:
        """Update ID index with new entries (incremental update).

        Args:
            collection_path: Path to collection directory
            updates: Dictionary of point IDs to file paths to add/update
        """
        with self._lock:
            # Load existing index
            existing_index = self.load_index(collection_path)

            # Merge updates
            existing_index.update(updates)

            # Save back to disk
            self.save_index(collection_path, existing_index)

    def remove_ids(self, collection_path: Path, point_ids: list) -> None:
        """Remove entries from ID index.

        Args:
            collection_path: Path to collection directory
            point_ids: List of point IDs to remove
        """
        with self._lock:
            # Load existing index
            existing_index = self.load_index(collection_path)

            # Remove specified IDs
            for point_id in point_ids:
                existing_index.pop(point_id, None)

            # Save back to disk
            self.save_index(collection_path, existing_index)

    def scan_vectors_for_id_map(self, collection_path: Path) -> Dict[str, Path]:
        """Side-effect-free scan of all vector JSON files -> point_id map.

        Story #1458 AC3 step 1: fleet migration MUST obtain the trustworthy
        point_id -> json_path map via THIS primitive, never via
        ``rebuild_from_vectors()`` -- that method additionally, as a side
        effect, atomically WRITES ``id_index.bin`` back to disk (see
        :meth:`rebuild_from_vectors`), which would silently RECREATE the
        exact file Story #1456 (AC1/AC7) requires be RETIRED for a
        consolidated (``chunks.db``) collection. This method NEVER reads or
        writes ``id_index.bin`` -- it only scans the ``vector_*.json``
        (and any other ``*.json``, excluding the well-known non-vector
        sidecars) files on disk.

        Backward-compatible bare-dict wrapper around
        :meth:`scan_vectors_for_id_map_verbose` -- existing callers
        (``rebuild_from_vectors`` and any other consumer) keep receiving
        exactly this return shape.

        Args:
            collection_path: Path to collection directory.

        Returns:
            Dictionary mapping point IDs to their originating file paths.
        """
        id_index, _rejected_count = self.scan_vectors_for_id_map_verbose(
            collection_path
        )
        return id_index

    def scan_vectors_for_id_map_verbose(
        self, collection_path: Path
    ) -> Tuple[Dict[str, Path], int]:
        """Side-effect-free scan of all vector JSON files -> point_id map,
        ALSO surfacing a distinct rejected-record count (Story #1458 Codex
        Finding #4, Messi Rule #13 anti-silent-failure).

        A genuinely-empty source directory and a directory where EVERY
        record was silently rejected as malformed both produce an empty
        id_map via the bare-dict :meth:`scan_vectors_for_id_map` -- callers
        that must distinguish the two (fleet migration: never flip the
        discriminator over silently-dropped data) call THIS method instead.
        ``rejected_count`` counts only GENUINE malformed-record rejections
        (JSON parse error, non-dict JSON, missing/invalid ``id`` field) --
        NOT the legitimate by-design skips (``collection_meta.json``,
        ``id_index.bin``, temporal bookkeeping sidecars), which are not
        vector records at all.

        Args:
            collection_path: Path to collection directory.

        Returns:
            ``(id_map, rejected_count)``.
        """
        import json

        id_index: Dict[str, Path] = {}
        rejected_count = 0
        # Codex round-6 CRITICAL finding #5: two distinct source files
        # sharing the same point_id must never be silently collapsed --
        # collect every conflicting path per duplicated id so we can fail
        # loud, naming all of them, once the scan completes.
        duplicate_paths: Dict[str, List[Path]] = {}

        # Scan all vector JSON files
        scanned_count = 0
        for json_file in collection_path.rglob("*.json"):
            if "collection_meta" in json_file.name:
                continue
            if json_file.name == self.INDEX_FILENAME:
                continue
            if json_file.name == _CHUNKS_DB_CONTENT_MANIFEST_FILENAME:
                continue
            if json_file.name in _TEMPORAL_BOOKKEEPING_FILENAMES:
                # Bug #1297: temporal marker/bookkeeping sidecars legitimately
                # lack an 'id' field -- skip silently, no WARNING, not a
                # rejection (not a vector record at all).
                continue
            if json_file.name == HNSW_SYNC_STATE_FILENAME:
                # Bug #1619: the dedicated hnsw_sync dirty-marker file lives
                # alongside collection_meta.json in the collection root --
                # a bookkeeping sidecar, never a vector record.
                continue

            scanned_count += 1
            try:
                with open(json_file) as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "scan_vectors_for_id_map: skipping %s — JSON parse error: %s",
                    json_file,
                    exc,
                )
                rejected_count += 1
                continue
            except OSError as exc:
                # Bug #1583 dual-review follow-up (opus LOW): a single
                # unreadable file (PermissionError -- e.g. a foreign-owned
                # file left behind under this project's documented
                # dual-OS-user server/auto-updater deployment, Bug #879)
                # must not abort the ENTIRE scan. Treated exactly like a
                # malformed record: logged, counted as a rejection, and
                # scanning continues over the rest of the collection.
                # json.JSONDecodeError (caught above) is a ValueError
                # subclass, never an OSError, so this clause cannot shadow
                # it.
                logger.warning(
                    "scan_vectors_for_id_map: skipping %s — unreadable: %s",
                    json_file,
                    exc,
                )
                rejected_count += 1
                continue

            if not isinstance(data, dict):
                logger.warning(
                    "scan_vectors_for_id_map: skipping %s — expected JSON object, got %s",
                    json_file,
                    type(data).__name__,
                )
                rejected_count += 1
                continue

            if "id" not in data:
                logger.warning(
                    "scan_vectors_for_id_map: skipping %s — missing 'id' field",
                    json_file,
                )
                rejected_count += 1
                continue

            point_id = data["id"]
            if not isinstance(point_id, str) or not point_id:
                logger.warning(
                    "scan_vectors_for_id_map: skipping %s — 'id' must be a non-empty str, got %r",
                    json_file,
                    point_id,
                )
                rejected_count += 1
                continue

            if point_id in id_index:
                duplicate_paths.setdefault(point_id, [id_index[point_id]]).append(
                    json_file
                )
                continue

            id_index[point_id] = json_file

        if duplicate_paths:
            details = "; ".join(
                f"{point_id!r} -> {[str(p) for p in paths]}"
                for point_id, paths in duplicate_paths.items()
            )
            raise DuplicateSourceIdError(
                f"scan_vectors_for_id_map: duplicate source point_id(s) "
                f"detected in {collection_path} -- {details} -- refusing "
                f"to silently pick a winner (would cause cleanup to "
                f"delete ALL conflicting source files while only one "
                f"survives); operator intervention required"
            )

        if not id_index and scanned_count > 0:
            logger.error(
                "scan_vectors_for_id_map: suspicious zero-entry scan — "
                "scanned %d vector files but produced no valid entries in %s "
                "(%d rejected as malformed)",
                scanned_count,
                collection_path,
                rejected_count,
            )

        return id_index, rejected_count

    def rebuild_from_vectors(
        self, collection_path: Path, *, self_heal: bool = False
    ) -> Dict[str, Path]:
        """Rebuild ID index by scanning all vector JSON files.

        Uses BackgroundIndexRebuilder for atomic file swapping with exclusive
        locking. Index loads can continue using old index during rebuild.

        Bug #1969: a legacy SHARDED_JSON collection can carry a duplicate
        point_id (two vector_*.json records sharing the same point_id but
        different content -- the pre-Bug-#1502 chunk-index collision
        shape), which makes the scan raise DuplicateSourceIdError. Unlike
        FilesystemVectorStore.scroll_points() (Bug #1579) and
        consolidate_collection_in_place(), this entry point had no
        self-heal, hard-aborting every caller (cidx index via
        FilesystemVectorStore._load_id_index(), and
        temporal_reconciliation.py's _reconcile_shard_legacy) with
        "operator intervention required" -- unacceptable on an unattended
        production deployment.

        Code review round 1 (F1, P2 BLOCKING): rebuild_from_vectors() is
        shared by BOTH write paths (cidx index / upsert / end-of-indexing /
        temporal reconciliation) AND query-time read paths (search,
        count_points, list_files, the daemon cache -- all via
        FilesystemVectorStore._load_id_index()'s corrupt-index branch). An
        unconditional self-heal there would let a plain query DELETE and
        rewrite vector_*.json files and rebuild the whole HNSW index from
        inside a query thread -- with no rollout gate, no query drain, and
        (worst case) targeting an immutable ``.versioned/`` snapshot, which
        this project's CLAUDE.md marks an absolute "NEVER modify/checkout/
        index inside .versioned/" invariant. Self-heal is therefore OPT-IN:
        ``self_heal`` defaults to False, so every existing caller that does
        not explicitly request it keeps the original hard-fail-immediately
        behavior (zero mutation, repair never attempted). Only genuine
        write-path callers (FilesystemVectorStore.upsert_points()/
        end_indexing() and temporal_reconciliation.py's
        _reconcile_shard_legacy) pass self_heal=True.

        When self_heal=True, mirror the scroll_points self-heal exactly:
        attempt repair_duplicate_and_shifted_points() ONCE, then retry the
        scan ONCE. A second DuplicateSourceIdError always propagates (the
        retry never re-attempts the repair), bounding this to at most one
        repair attempt per call -- never an infinite loop.
        DedupRepairAmbiguousError (e.g. a malformed record the repair
        refuses to touch) is deliberately NOT caught here -- it is more
        specific and actionable than DuplicateSourceIdError and must
        propagate to the caller unchanged. Defense-in-depth: even with
        self_heal=True, an immutable versioned snapshot path is NEVER
        mutated -- the original DuplicateSourceIdError propagates instead.

        Bug #1969 Round 3 (the REAL corrupt-index self-heal): code review
        round 2 found that in the EXACT bug-report scenario -- a corrupt/
        unreadable id_index.bin -- repair_duplicate_and_shifted_points()'s
        own AC30 safety rule (_plan_dedup) can never pick a per-chunk
        winner, because it needs to read the very id_index.bin that is
        corrupt to know "who is currently being served". It raises
        DedupRepairAmbiguousError(reason=DedupRepairAmbiguousReason.
        CORRUPT_ID_INDEX) instead of DuplicateSourceIdError -- a better-
        labeled hard failure, but still a hard failure, not a recovery.
        This method now escalates ONLY that one specific reason: it calls
        collection_dedup_repair.recover_from_corrupt_id_index_by_wiping_
        files(), which deletes EVERY vector_*.json record belonging to
        EVERY file implicated by any duplicate point_id group (not just
        the colliding chunks -- see that function's docstring for why a
        wholly-missing file is the safe outcome for this project's
        smart_indexer.py reconcile, which has no notion of "partially
        indexed"), then rebuilds id_index.bin/HNSW from the remaining,
        conflict-free records. The scan is then retried ONE more time --
        the SAME bounded-retry slot the normal repair uses, never an
        additional one. Every OTHER DedupRepairAmbiguousError reason
        (malformed record, foreign identity scheme, undeterminable HNSW
        parameter, etc.) still propagates immediately, unchanged from
        round 2 -- those signal data whose identity assumptions this
        repair cannot trust, and auto-resolving them would be unsafe.

        Args:
            collection_path: Path to collection directory
            self_heal: opt-in for the one-shot dedup-repair self-heal
                (see above). Defaults to False -- callers on a read/query
                path must never pass True.

        Returns:
            Dictionary mapping point IDs to file paths
        """
        from .background_index_rebuilder import BackgroundIndexRebuilder

        try:
            id_index = self.scan_vectors_for_id_map(collection_path)
        except DuplicateSourceIdError:
            if not self_heal:
                raise

            # Defense-in-depth: never mutate an immutable .versioned/
            # snapshot, even though a genuine write-path caller should
            # never target one in the first place (Bug #1969 F1).
            from code_indexer.server.services.query_path_cache import (
                is_immutable_versioned_snapshot,
            )

            if is_immutable_versioned_snapshot(str(collection_path)):
                logger.warning(
                    "Bug #1969: rebuild_from_vectors hit a duplicate "
                    "point_id inside an immutable versioned snapshot %r -- "
                    "refusing to self-heal a snapshot; propagating the "
                    "original error.",
                    collection_path,
                )
                raise

            from code_indexer.storage.shared.collection_dedup_repair import (
                DedupRepairAmbiguousError,
                DedupRepairAmbiguousReason,
                recover_from_corrupt_id_index_by_wiping_files,
                repair_duplicate_and_shifted_points,
            )

            logger.warning(
                "Bug #1969: rebuild_from_vectors hit a duplicate point_id "
                "while scanning collection %r -- attempting a one-shot "
                "dedup repair before failing the rebuild.",
                collection_path,
            )
            try:
                repair_duplicate_and_shifted_points(collection_path)
            except DedupRepairAmbiguousError as ambiguous_exc:
                if ambiguous_exc.reason != DedupRepairAmbiguousReason.CORRUPT_ID_INDEX:
                    # Every OTHER reason (malformed record, foreign
                    # identity scheme, undeterminable HNSW parameter,
                    # etc.) signals data whose identity assumptions this
                    # repair cannot trust -- more specific/actionable than
                    # DuplicateSourceIdError, must propagate unchanged.
                    raise
                # Bug #1969 Round 3: the ONE reason safe to escalate --
                # id_index.bin itself failed to load, so the repair's own
                # winner-lookup had no trustworthy reference. Fall back to
                # the whole-file-wipe recovery, which does not depend on
                # id_index.bin at all. This is the SAME bounded-retry slot
                # as the normal repair -- a failure from the escalation
                # itself propagates below, never triggering another
                # attempt.
                logger.warning(
                    "Bug #1969 Round 3: repair_duplicate_and_shifted_"
                    "points could not resolve %r (id_index.bin itself is "
                    "corrupt/unreadable, AC30) -- escalating to whole-"
                    "file-wipe recovery.",
                    collection_path,
                )
                recover_from_corrupt_id_index_by_wiping_files(collection_path)
            # Retry exactly once -- a second failure of any kind
            # propagates unchanged (bounded, never a loop), whether the
            # repair above ran normally or was escalated to the whole-
            # file wipe.
            id_index = self.scan_vectors_for_id_map(collection_path)

        # Use BackgroundIndexRebuilder for atomic swap with locking
        rebuilder = BackgroundIndexRebuilder(collection_path)
        index_file = collection_path / self.INDEX_FILENAME

        def build_id_index_to_temp(temp_file: Path) -> None:
            """Build ID index to temp file."""
            with open(temp_file, "wb") as f:
                # Write number of entries (4 bytes, uint32)
                f.write(struct.pack("<I", len(id_index)))

                # Write each entry
                for point_id, file_path in id_index.items():
                    # Make path relative to collection_path
                    try:
                        relative_path = file_path.relative_to(collection_path)
                        path_str = str(relative_path)
                    except ValueError:
                        # If path is not relative to collection_path, store as-is
                        path_str = str(file_path)

                    # Encode strings to UTF-8
                    id_bytes = point_id.encode("utf-8")
                    path_bytes = path_str.encode("utf-8")

                    # Write ID length (2 bytes, uint16) and ID string
                    f.write(struct.pack("<H", len(id_bytes)))
                    f.write(id_bytes)

                    # Write path length (2 bytes, uint16) and path string
                    f.write(struct.pack("<H", len(path_bytes)))
                    f.write(path_bytes)

        # Rebuild with lock (entire rebuild duration)
        rebuilder.rebuild_with_lock(build_id_index_to_temp, index_file)

        return id_index
