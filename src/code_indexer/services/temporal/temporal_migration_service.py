"""Background migration of monolithic temporal HNSW indexes to quarterly shards.

Story #1172: When the server starts up, it detects repos that have monolithic
temporal HNSW indexes (pre-sharding) and submits background migration jobs to
convert them to quarterly shard layout. No re-embedding — vectors are extracted
from existing HNSW using hnswlib.Index.get_items() and redistributed.

Key constraints:
- NO re-embedding: use hnswlib.Index.get_items(labels) for vector extraction
- NO hard links: JSON payload files copied via shutil.copy2()
- Sequential per-collection: process one monolithic collection at a time
- Explicit memory free: del monolithic_index; gc.collect() after each collection
"""

import gc
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from code_indexer.services.temporal.temporal_collection_naming import (
    has_real_monolith,
    is_sharded_temporal_collection,
    is_temporal_collection,
    quarter_suffix,
)

logger = logging.getLogger(__name__)

# Marker written inside the monolithic collection dir after all shards complete.
MIGRATION_COMPLETE_MARKER = "migration_complete.marker"
# Suffix appended to shard dir name during in-progress writes.
MIGRATING_SUFFIX = ".migrating"

# HNSW construction parameters (match existing code defaults).
_HNSW_M = 16
_HNSW_EF_CONSTRUCTION = 200
# Maximum SHAs per git log call to avoid E2BIG on large repos (Bug #1238).
# At 41 bytes/SHA (40 hex + space), 1000 SHAs ≈ 41 KB — well within ARG_MAX.
_SHA_CHUNK_SIZE = 1000
# Upper bound on element count when loading a monolithic HNSW index.
_MAX_MONOLITHIC_ELEMENTS = 2_000_000
# Default vector dimension and space when metadata is missing.
_DEFAULT_VECTOR_DIM = 1024
_DEFAULT_SPACE = "cosine"
# Binary format widths for id_index.bin.
_ID_INDEX_HEADER_BYTES = 4  # uint32 entry-count
_ID_INDEX_LENGTH_BYTES = 2  # uint16 string-length fields


def _needs_temporal_migration(index_path: Path) -> bool:
    """Return True if index_path contains any unsharded temporal collection.

    A collection needs migration when ALL of:
    - It is a temporal collection (is_temporal_collection() == True)
    - It is NOT already a sharded collection (is_sharded_temporal_collection() == False)
    - It does NOT have migration_complete.marker
    - It HAS hnsw_index.bin (there is a monolithic HNSW to migrate)

    Args:
        index_path: Path to the .code-indexer/index/ directory.

    Returns:
        True if at least one collection needs migration.
    """
    if not index_path or not index_path.exists():
        return False

    for entry in index_path.iterdir():
        if not entry.is_dir():
            continue
        if not is_temporal_collection(entry.name):
            continue
        if is_sharded_temporal_collection(entry.name):
            continue
        # Bug #1207 BLOCKER 3: use the single shared predicate has_real_monolith()
        # instead of duplicating the marker/hnsw check here.  Both callers now agree
        # on what constitutes a real unmigrated monolith — one source of truth.
        if has_real_monolith(entry):
            return True
    return False


# ---------------------------------------------------------------------------
# Git-based timestamp helpers (production correctness)
# ---------------------------------------------------------------------------

# Regex matching a valid 40-character lowercase hex SHA-1.
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _extract_sha_from_point_id(point_id: str) -> Optional[str]:
    """Extract the commit SHA from a real-format point_id.

    Real point_id formats:
        {repo}:commit:{sha40}:{idx}
        {repo}:diff:{sha40}:{file_path}:{chunk_idx}

    The SHA is always the 3rd colon-separated field (index 2).

    Returns:
        40-char lowercase hex SHA string, or None if the point_id is
        synthetic (test data), too short, or the field is not a valid SHA-1.
    """
    if not point_id:
        return None
    parts = point_id.split(":")
    if len(parts) < 4:
        return None
    candidate = parts[2]
    if not _SHA1_RE.match(candidate):
        return None
    return candidate


def _parse_git_log_stdout(stdout: str, repo_path: Path) -> Dict[str, datetime]:
    """Parse ``git log --no-walk --format="%H %cI"`` stdout into {sha: datetime}.

    Shared by both the batched call and the per-SHA retry fallback in
    :func:`_batch_get_commit_timestamps` so the two paths cannot drift apart.
    """
    result: Dict[str, datetime] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            # Expected format: "<40-char-sha> <ISO-8601-strict>"
            # e.g.  "2421d586942eb5c4eca700fbf6bfc0c99af679ef 2024-03-15T10:22:44+00:00"
            # git 2.x emits trailing Z for UTC commits (e.g. "2026-03-25T05:18:38Z").
            # Python 3.9 fromisoformat() cannot parse the Z suffix — only 3.11+ can.
            # Normalise Z -> +00:00 before parsing so all Python 3.x versions work.
            space_idx = line.index(" ")
            sha = line[:space_idx]
            ts_str = line[space_idx + 1 :]
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
            result[sha] = dt
        except Exception as exc:
            logger.debug(
                "Migration: skipping unparseable git log line %r for %s: %s",
                line,
                repo_path,
                exc,
            )
    return result


def _batch_get_commit_timestamps(
    repo_path: Path,
    shas: Set[str],
) -> Dict[str, datetime]:
    """Return a mapping of {sha: UTC datetime} for the given set of SHAs.

    Uses a single ``git log`` subprocess call for all SHAs in a chunk.  Any
    SHAs that git does not recognise (e.g. not in the repo, or a fake SHA)
    are silently omitted from the result.

    Bug #1286 follow-up (empirically confirmed): ``git log --no-walk sha1
    sha2 ... shaN`` resolves ALL revision arguments atomically BEFORE
    producing any output — if even ONE SHA in the chunk is unresolvable (e.g.
    from a rebase/squash/force-push/gc rewriting history), the entire command
    fails (non-zero exit, EMPTY stdout) and drops timestamps for every OTHER
    valid SHA in that same chunk too, not just the bad one. On a chunk
    failure this function now retries each SHA in the chunk individually so a
    single unresolvable SHA cannot poison its siblings.

    Args:
        repo_path: Path to the git repository root.
        shas: Set of 40-char SHA strings to look up.

    Returns:
        Dict mapping each found SHA to its author datetime in UTC.
        Returns empty dict on any error (non-fatal).
    """
    if not shas or not repo_path or not repo_path.exists():
        return {}

    # git log --no-walk accepts multiple SHA arguments; output one line per SHA.
    # Bug #1238: chunk SHAs to avoid E2BIG on large repos.
    # At ~41 bytes/SHA, _SHA_CHUNK_SIZE=1000 -> ~41 KB per call, well under ARG_MAX.
    result: Dict[str, datetime] = {}
    sorted_shas = sorted(shas)

    for chunk_start in range(0, len(sorted_shas), _SHA_CHUNK_SIZE):
        chunk = sorted_shas[chunk_start : chunk_start + _SHA_CHUNK_SIZE]
        try:
            proc = subprocess.run(
                ["git", "log", "--no-walk", "--format=%H %cI"] + chunk,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:
            logger.warning(
                "Migration: git log failed for %s (chunk %d-%d): %s — skipping chunk",
                repo_path,
                chunk_start,
                chunk_start + len(chunk),
                exc,
            )
            continue

        if proc.returncode != 0:
            # Bug #1286: the batch call failed — most commonly because ONE SHA in
            # this chunk is unresolvable (rewritten/rebased/squashed/gc'd history).
            # git aborts the WHOLE call in that case, so retry one SHA at a time —
            # bounded by len(chunk) (<= _SHA_CHUNK_SIZE) — to recover every OTHER
            # valid SHA's timestamp instead of losing the entire chunk.
            logger.debug(
                "Migration: batched git log failed for %s (chunk %d-%d, "
                "returncode=%d) — retrying %d SHA(s) individually",
                repo_path,
                chunk_start,
                chunk_start + len(chunk),
                proc.returncode,
                len(chunk),
            )
            for sha in chunk:
                try:
                    single_proc = subprocess.run(
                        ["git", "log", "--no-walk", "--format=%H %cI", sha],
                        cwd=str(repo_path),
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                except Exception as exc:
                    logger.debug(
                        "Migration: per-SHA git log retry failed for %s in %s: %s",
                        sha,
                        repo_path,
                        exc,
                    )
                    continue
                if single_proc.returncode != 0:
                    # This specific SHA genuinely does not resolve (e.g. truly
                    # rewritten out of history) — leave it absent; the caller's
                    # payload-commit_timestamp fallback handles it.
                    continue
                result.update(_parse_git_log_stdout(single_proc.stdout, repo_path))
            continue

        result.update(_parse_git_log_stdout(proc.stdout, repo_path))

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_id_mapping_from_meta(collection_path: Path) -> Dict[int, str]:
    """Load label->point_id mapping from collection_meta.json hnsw_index section.

    Raises:
        ValueError: If the metadata file is missing, corrupt, or has no hnsw_index.
    """
    meta_file = collection_path / "collection_meta.json"
    if not meta_file.exists():
        raise ValueError(f"collection_meta.json not found at {collection_path}")
    with open(meta_file) as f:
        metadata = json.load(f)
    id_mapping_str = metadata.get("hnsw_index", {}).get("id_mapping", {})
    return {int(k): v for k, v in id_mapping_str.items()}


def _load_id_index_bin(collection_path: Path) -> Dict[str, str]:
    """Load id_index.bin -> {point_id: relative_json_path}.

    Returns:
        Dict mapping point_id to relative path string within collection dir.

    Raises:
        ValueError: If the binary file is missing, truncated, or corrupt.
    """
    index_file = collection_path / "id_index.bin"
    if not index_file.exists():
        raise ValueError(f"id_index.bin not found at {collection_path}")

    result: Dict[str, str] = {}
    with open(index_file, "rb") as f:
        header = f.read(_ID_INDEX_HEADER_BYTES)
        if len(header) < _ID_INDEX_HEADER_BYTES:
            raise ValueError(f"id_index.bin at {collection_path} is too short (header)")
        num_entries = struct.unpack("<I", header)[0]
        for _ in range(num_entries):
            id_len_b = f.read(_ID_INDEX_LENGTH_BYTES)
            if len(id_len_b) < _ID_INDEX_LENGTH_BYTES:
                raise ValueError(
                    f"id_index.bin at {collection_path} is truncated (id_len)"
                )
            id_len = struct.unpack("<H", id_len_b)[0]
            point_id = f.read(id_len).decode("utf-8")
            path_len_b = f.read(_ID_INDEX_LENGTH_BYTES)
            if len(path_len_b) < _ID_INDEX_LENGTH_BYTES:
                raise ValueError(
                    f"id_index.bin at {collection_path} is truncated (path_len)"
                )
            path_len = struct.unpack("<H", path_len_b)[0]
            path_str = f.read(path_len).decode("utf-8")
            result[point_id] = path_str
    return result


def _write_id_index_bin(dest_path: Path, id_index: Dict[str, Path]) -> None:
    """Write id_index.bin at dest_path.  Atomic via temp+rename."""
    collection_path = dest_path.parent
    temp_path = dest_path.with_suffix(".bin.tmp")
    with open(temp_path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, file_path in id_index.items():
            try:
                rel = str(file_path.relative_to(collection_path))
            except ValueError:
                rel = str(file_path)
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)
    os.replace(temp_path, dest_path)


def _write_collection_meta(
    shard_dir: Path,
    shard_name: str,
    vector_dim: int,
    space: str,
    id_mapping: Dict[int, str],
    quantization_range: Optional[Dict[str, float]] = None,
) -> None:
    """Write collection_meta.json for a shard directory. Atomic via temp+rename."""
    hnsw_file = shard_dir / "hnsw_index.bin"
    file_size = hnsw_file.stat().st_size if hnsw_file.exists() else 0
    meta: Dict[str, Any] = {
        "name": shard_name,
        "vector_size": vector_dim,
        "created_at": datetime.utcnow().isoformat(),
        "hnsw_index": {
            "version": 1,
            "vector_count": len(id_mapping),
            "vector_dim": vector_dim,
            "M": _HNSW_M,
            "ef_construction": _HNSW_EF_CONSTRUCTION,
            "space": space,
            "last_rebuild": datetime.utcnow().isoformat(),
            "file_size_bytes": file_size,
            "id_mapping": {str(label): pid for label, pid in id_mapping.items()},
            "is_stale": False,
            "last_marked_stale": None,
        },
    }
    if quantization_range is not None:
        meta["quantization_range"] = quantization_range
    meta_path = shard_dir / "collection_meta.json"
    tmp = meta_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, meta_path)


def _get_vector_dim_and_space(collection_path: Path) -> Tuple[int, str]:
    """Read vector dimension and space from collection_meta.json.

    Falls back to defaults and logs a WARNING when the file is missing or corrupt.
    """
    meta_file = collection_path / "collection_meta.json"
    if not meta_file.exists():
        logger.warning(
            "Migration: collection_meta.json missing at %s — "
            "using default dim=%d space=%s",
            collection_path,
            _DEFAULT_VECTOR_DIM,
            _DEFAULT_SPACE,
        )
        return _DEFAULT_VECTOR_DIM, _DEFAULT_SPACE
    try:
        with open(meta_file) as f:
            metadata = json.load(f)
        dim = metadata.get("hnsw_index", {}).get(
            "vector_dim", metadata.get("vector_size", _DEFAULT_VECTOR_DIM)
        )
        space = metadata.get("hnsw_index", {}).get("space", _DEFAULT_SPACE)
        return int(dim), str(space)
    except Exception as exc:
        logger.warning(
            "Migration: failed to read collection_meta.json at %s: %s — "
            "using default dim=%d space=%s",
            collection_path,
            exc,
            _DEFAULT_VECTOR_DIM,
            _DEFAULT_SPACE,
        )
        return _DEFAULT_VECTOR_DIM, _DEFAULT_SPACE


def _build_quarter_buckets(
    collection_path: Path,
    label_to_point_id: Dict[int, str],
    point_id_to_rel_path: Dict[str, str],
    sha_timestamps: Optional[Dict[str, datetime]] = None,
) -> Tuple[Dict[str, List[Tuple[int, str, Path]]], Dict[str, int]]:
    """Group vectors into quarterly buckets based on commit timestamp.

    Timestamp resolution strategy (primary → fallback), Bug #1286 follow-up:
    1. commit_timestamp field inside the JSON payload file — PRIMARY. Every
       temporal payload has carried this field unconditionally since v7.x; it
       is immutable, captured once at index time.
    2. sha_timestamps dict keyed by commit SHA (from git log) — FALLBACK, used
       only when the payload lacks commit_timestamp (legacy/pre-v7.x payloads,
       or a corrupt/unreadable payload file). git history is mutable
       (rebase/squash/force-push/gc), so it is deliberately NOT authoritative
       over the payload's stored value.

    Args:
        collection_path: Monolithic collection directory.
        label_to_point_id: {int_label: point_id} from collection_meta.json.
        point_id_to_rel_path: {point_id: rel_json_path} from id_index.bin.
        sha_timestamps: Optional pre-built {sha: datetime_utc} from git log,
            consulted only as a fallback when the JSON payload has no
            commit_timestamp.

    Returns:
        Tuple of (buckets, drop_counts) where:
        - buckets maps quarter_str (e.g. "2024Q3") to list of
          (label, point_id, src_json_path) tuples.
        - drop_counts maps drop reason to count:
            "missing_id_index"    — point_id not in id_index.bin (structural orphan)
            "missing_json"        — id_index entry exists but JSON file gone (structural orphan)
            "timestamp_unresolved"— both git and payload timestamps absent (recoverable)
    """
    buckets: Dict[str, List[Tuple[int, str, Path]]] = {}
    drop_counts: Dict[str, int] = {
        "missing_id_index": 0,
        "missing_json": 0,
        "timestamp_unresolved": 0,
    }
    _sha_ts = sha_timestamps or {}

    for label, point_id in label_to_point_id.items():
        rel_path = point_id_to_rel_path.get(point_id)
        if rel_path is None:
            logger.debug(
                "Migration: point_id %s has no id_index entry in %s — skipping",
                point_id,
                collection_path.name,
            )
            drop_counts["missing_id_index"] += 1
            continue

        src_json = collection_path / rel_path
        if not src_json.exists():
            logger.debug(
                "Migration: JSON payload %s not found — skipping point %s",
                src_json,
                point_id,
            )
            drop_counts["missing_json"] += 1
            continue

        # Bug #1286 follow-up: PAYLOAD commit_timestamp is now the PRIMARY
        # timestamp source, git log the FALLBACK. Every temporal payload has
        # carried payload.commit_timestamp unconditionally since v7.x — it is
        # captured once at index time and is immutable. git history is
        # mutable: a rebase/squash/force-push/gc can make a commit SHA
        # unresolvable via `git log` even though the vector was correctly
        # embedded and its payload correctly stamped. Preferring the payload
        # avoids falsely hard-aborting a healthy index just because its
        # git history was later rewritten. A corrupt/unreadable payload or an
        # invalid commit_ts value degrades gracefully to the git fallback
        # rather than aborting the point outright.
        commit_ts = None
        try:
            with open(src_json) as f:
                payload_data = json.load(f)
            commit_ts = payload_data.get("payload", {}).get("commit_timestamp")
            if commit_ts is None:
                commit_ts = payload_data.get("commit_timestamp")
        except Exception as exc:
            logger.debug(
                "Migration: could not read commit_timestamp from %s: %s — "
                "falling back to git timestamp for point %s",
                src_json,
                exc,
                point_id,
            )

        dt: Optional[datetime] = None
        if commit_ts is not None:
            try:
                dt = datetime.fromtimestamp(int(commit_ts), tz=timezone.utc)
            except Exception as exc:
                logger.debug(
                    "Migration: invalid commit_timestamp %r in %s: %s — "
                    "falling back to git timestamp for point %s",
                    commit_ts,
                    src_json,
                    exc,
                    point_id,
                )
                dt = None

        if dt is None:
            # Fallback: git-derived commit timestamp (legacy/pre-v7.x payloads
            # that never stored commit_timestamp, or a corrupt payload above).
            sha = _extract_sha_from_point_id(point_id)
            dt = _sha_ts.get(sha) if sha else None

        if dt is None:
            logger.debug(
                "Migration: no commit_timestamp in %s and no git timestamp "
                "for point %s — skipping",
                src_json,
                point_id,
            )
            drop_counts["timestamp_unresolved"] += 1
            continue

        q = quarter_suffix(dt)
        buckets.setdefault(q, []).append((label, point_id, src_json))

    return buckets, drop_counts


def _read_monolith_quantization_range(
    collection_path: Path, vector_dim: int
) -> Dict[str, float]:
    """Read quantization_range from monolith collection_meta.json.

    Falls back to the create_collection formula (±3·sqrt(64/input_dim)) when the
    monolith meta is absent, corrupt, or missing the key.

    Args:
        collection_path: Monolith (or base) collection directory.
        vector_dim: Full-dimension vector size (used for formula fallback).

    Returns:
        Dict with "min" and "max" float keys.
    """
    try:
        meta_path = collection_path / "collection_meta.json"
        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
            qr = meta.get("quantization_range")
            if qr and "min" in qr and "max" in qr:
                return {"min": float(qr["min"]), "max": float(qr["max"])}
    except Exception:
        pass
    # Formula from FilesystemVectorStore.create_collection: ±3·sqrt(output_dim/input_dim)
    output_dim = 64
    std = math.sqrt(output_dim / vector_dim)
    return {"min": float(-3 * std), "max": float(3 * std)}


def _backfill_quantization_range_if_missing(
    shard_path: Path,
    source_collection_path: Optional[Path],
    vector_dim: int,
) -> None:
    """Write quantization_range into shard meta if not already present. Atomic.

    Args:
        shard_path: Shard directory whose collection_meta.json may need updating.
        source_collection_path: Base collection to try reading quantization_range from.
            Falls back to formula if None or if source meta lacks the key.
        vector_dim: Full input dimension (for formula fallback).
    """
    meta_path = shard_path / "collection_meta.json"
    if not meta_path.exists():
        return
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception as exc:
        logger.warning(
            "Bug #1242: could not read shard meta at %s: %s", shard_path, exc
        )
        return

    if "quantization_range" in meta:
        return  # Already present — nothing to do

    src = source_collection_path if source_collection_path is not None else shard_path
    qr = _read_monolith_quantization_range(src, vector_dim)
    meta["quantization_range"] = qr
    tmp = meta_path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, meta_path)
        logger.debug("Bug #1242: backfilled quantization_range in %s", shard_path.name)
    except Exception as exc:
        logger.warning(
            "Bug #1242: failed to backfill quantization_range in %s: %s",
            shard_path.name,
            exc,
        )


def _atomic_replace_via_tmp(final_path: Path, write_fn: Callable[[Path], None]) -> None:
    """Write final_path atomically: write_fn populates a uniquely-named temp
    file in the SAME directory, then os.replace() renames it onto final_path.

    Bug #1264 (code review follow-up): closes the torn-read window where a
    concurrent reader/healer of the same target path could observe a
    partially-written file. os.replace() is atomic on POSIX and overwrites
    the destination, so any concurrent observer sees either the old (absent)
    state or the fully-written new file -- never something in between. The
    temp filename is unique per call (uuid4) so two concurrent callers
    writing the same final_path never collide on the SAME temp file either.

    Args:
        final_path: Destination file path (parent directory must exist).
        write_fn: Callable that fully writes the temp path given as its
            single argument. Must not partially write on success.
    """
    tmp_path = final_path.parent / f"{final_path.name}.tmp.{uuid.uuid4().hex}"
    try:
        write_fn(tmp_path)
        os.replace(tmp_path, final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _ensure_shard_has_projection_matrix(
    shard_path: Path,
    source_collection_path: Optional[Path],
    vector_dim: int,
) -> None:
    """Ensure shard_path has projection_matrix.npy (copy from source or regenerate).

    Idempotent — returns immediately when the matrix file is already present.

    Preferred: copies from source_collection_path (monolith or base collection) so
    the write-path bucket layout (vector @ matrix -> hex dir) is consistent with
    any vectors previously written by the monolith.  Falls back to regenerating a
    fresh matrix when the source is None or lacks the file.

    Both branches write atomically (temp file in the same directory, then
    os.replace()) so two callers healing the same shard concurrently — e.g.
    two temporal worker threads first-writing the same shard — never produce
    or observe a torn/partial projection_matrix.npy (Bug #1264 follow-up).

    Also backfills quantization_range in the shard's collection_meta.json when the
    key is missing (both copy and regenerate paths).

    Args:
        shard_path: Shard (or in-progress migrating) directory.
        source_collection_path: Monolith / base collection to copy from, or None.
        vector_dim: Full input dimension (e.g. 1024) for regeneration + formula.
    """
    from code_indexer.storage.projection_matrix_manager import ProjectionMatrixManager

    matrix_file = shard_path / "projection_matrix.npy"
    if matrix_file.exists():
        return  # Already present — idempotent

    copied = False
    if source_collection_path is not None:
        src_matrix = source_collection_path / "projection_matrix.npy"
        if src_matrix.exists():

            def _copy_matrix(tmp: Path, _src: Path = src_matrix) -> None:
                shutil.copy2(_src, tmp)

            _atomic_replace_via_tmp(matrix_file, _copy_matrix)
            logger.info(
                "Bug #1242: copied projection_matrix.npy from %s into %s",
                source_collection_path.name,
                shard_path.name,
            )
            copied = True

    if not copied:
        output_dim = 64
        manager = ProjectionMatrixManager()
        matrix = manager.create_projection_matrix(
            input_dim=vector_dim, output_dim=output_dim
        )

        def _write_matrix(tmp: Path) -> None:
            with open(tmp, "wb") as f:
                np.save(f, matrix)

        _atomic_replace_via_tmp(matrix_file, _write_matrix)
        logger.info(
            "Bug #1242: regenerated projection_matrix.npy for %s (source absent)",
            shard_path.name,
        )

    # Backfill quantization_range in meta if absent (no-op when already written)
    _backfill_quantization_range_if_missing(
        shard_path, source_collection_path, vector_dim
    )


def _build_one_shard(
    collection_path: Path,
    index_path: Path,
    shard_name: str,
    entries: List[Tuple[int, str, Path]],
    monolithic_index: Any,
    vector_dim: int,
    space: str,
) -> None:
    """Build one quarterly shard directory atomically.

    Writes to {shard_name}.migrating then renames to shard_name.
    Skips if the final shard dir's collection_meta.json already exists (idempotent).

    Args:
        collection_path: Monolithic collection directory.
        index_path: Parent index directory.
        shard_name: Final shard collection name (e.g. code-indexer-temporal-voyage_code_3-2024Q1).
        entries: List of (label, point_id, src_json_path) for this shard.
        monolithic_index: Loaded hnswlib.Index from the monolithic collection.
        vector_dim: Vector dimension.
        space: Distance metric.
    """
    import hnswlib

    final_shard_dir = index_path / shard_name
    migrating_dir = index_path / f"{shard_name}{MIGRATING_SUFFIX}"

    # AC5: Skip if final shard already exists (idempotent on restart).
    if (final_shard_dir / "collection_meta.json").exists():
        logger.debug("Migration: shard %s already exists — skipping", shard_name)
        return

    # AC4: Remove stale .migrating dir.
    if migrating_dir.exists():
        shutil.rmtree(migrating_dir)

    migrating_dir.mkdir(parents=True, exist_ok=True)

    # Copy JSON files; build shard id_index.
    shard_id_index: Dict[str, Path] = {}
    shard_labels: List[int] = []
    shard_point_ids: List[str] = []

    for label, point_id, src_json in entries:
        try:
            rel = src_json.relative_to(collection_path)
        except ValueError:
            rel = Path(src_json.name)
        dst_json = migrating_dir / rel
        dst_json.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_json, dst_json)  # AC constraint: copy, not hard-link
        shard_id_index[point_id] = dst_json
        shard_labels.append(label)
        shard_point_ids.append(point_id)

    _write_id_index_bin(migrating_dir / "id_index.bin", shard_id_index)

    # Extract vectors from monolithic index (no re-embedding).
    vectors_array = monolithic_index.get_items(shard_labels)  # type: ignore[union-attr]
    vectors_np = np.array(vectors_array, dtype=np.float32)
    n = len(shard_labels)

    # Build shard HNSW with new sequential labels starting from 0.
    shard_index = hnswlib.Index(space=space, dim=vector_dim)
    shard_index.init_index(
        max_elements=n,
        M=_HNSW_M,
        ef_construction=_HNSW_EF_CONSTRUCTION,
        allow_replace_deleted=True,
    )
    shard_index.add_items(vectors_np, np.arange(n))
    shard_index.save_index(str(migrating_dir / "hnsw_index.bin"))

    new_id_mapping = {i: shard_point_ids[i] for i in range(n)}

    # Bug #1242: read quantization_range from the monolith to include in shard meta.
    _quant_range = _read_monolith_quantization_range(collection_path, vector_dim)

    _write_collection_meta(
        shard_dir=migrating_dir,
        shard_name=shard_name,
        vector_dim=vector_dim,
        space=space,
        id_mapping=new_id_mapping,
        quantization_range=_quant_range,
    )

    # Bug #1242: copy projection_matrix.npy from monolith (or regenerate as fallback)
    # before the atomic rename so the shard is immediately usable by upsert_points.
    _ensure_shard_has_projection_matrix(migrating_dir, collection_path, vector_dim)

    # Atomic rename to final shard dir.
    os.replace(migrating_dir, final_shard_dir)


def _verify_migration_lossless_and_complete(
    collection_name: str,
    index_path: Path,
    label_to_point_id: Dict[int, str],
    quarter_buckets: Dict[str, List[Tuple[int, str, Path]]],
    vectors_migrated: int,
) -> None:
    """Defensive invariant gate (Bug #1286 defect 2): NEVER write the completion
    marker or delete the monolith unless the migration is PROVABLY lossless and
    complete.

    This is an INDEPENDENT line of defense (Messi #15 defensive invariants), not
    merely a restatement of the abort-on-any-drop guard in
    :func:`_migrate_one_collection`. Even if a future code change accidentally
    lets a dropped/miscounted point slip past that guard, this gate is the last
    checkpoint before the monolith is destroyed.

    Args:
        collection_name: Base (monolithic) collection name.
        index_path: Parent index directory containing shard subdirectories.
        label_to_point_id: The full source label->point_id mapping (count-in).
        quarter_buckets: The buckets that were built into shards (count-out grouping).
        vectors_migrated: Number of vectors actually written across all shards.

    Raises:
        RuntimeError: If vectors_migrated does not exactly equal the number of
            source points (count-in != count-out), or any quarter bucket lacks
            a completed shard directory (collection_meta.json) on disk.
    """
    expected = len(label_to_point_id)
    if vectors_migrated != expected:
        raise RuntimeError(
            f"Migration aborted for {collection_name}: count mismatch before "
            f"finalize — {vectors_migrated} vectors migrated but {expected} "
            f"expected (count-in != count-out). Monolithic index preserved "
            f"untouched; migration_complete.marker NOT written."
        )

    for q_str in quarter_buckets:
        shard_name = f"{collection_name}-{q_str}"
        shard_meta = index_path / shard_name / "collection_meta.json"
        if not shard_meta.exists():
            raise RuntimeError(
                f"Migration aborted for {collection_name}: expected shard "
                f"'{shard_name}' is missing collection_meta.json after the shard "
                f"build loop reported success. Monolithic index preserved "
                f"untouched; migration_complete.marker NOT written."
            )


def _cleanup_monolithic_collection(collection_path: Path) -> None:
    """Write migration marker; delete monolithic binaries and vector payload files.

    Bug #1286 defect 3: JSON deletion is scoped to the precise payload-file
    naming convention "vector_*.json" (matching temporal_reconciliation.py and
    FilesystemVectorStore's real production payload naming,
    e.g. filesystem_vector_store.py:1246/1250/1686) instead of a blanket
    "*.json minus collection_meta.json" glob. The blanket glob also matched
    (and permanently destroyed) bookkeeping files that are NOT vector payloads:
    temporal_progress.json (crash-resume completed-commits tracker) and
    temporal_meta.json (last_indexed_commit anchor used by
    TemporalIndexer._load_last_indexed_commit for incremental indexing).
    Destroying those files forces the next indexing run to lose its
    incremental-resume anchor and fall back to a full git-history walk —
    exactly the "expensive recovery ignores intact source" failure mode this
    bug report describes.
    """
    # Step 6: Write migration_complete.marker.
    (collection_path / MIGRATION_COMPLETE_MARKER).write_text("migration complete\n")

    # Step 7: Delete monolithic hnsw_index.bin and id_index.bin.
    for fname in ("hnsw_index.bin", "id_index.bin"):
        p = collection_path / fname
        if p.exists():
            p.unlink()

    # Step 8: Delete monolithic vector payload JSON files ONLY (never bookkeeping
    # files such as collection_meta.json, temporal_progress.json, temporal_meta.json).
    for json_file in list(collection_path.rglob("vector_*.json")):
        try:
            json_file.unlink()
        except OSError as exc:
            logger.warning("Migration: failed to delete %s: %s", json_file, exc)


def _migrate_one_collection(
    collection_path: Path,
    index_path: Path,
    progress_callback: Optional[Callable[[str], None]],
    repo_path: Optional[Path] = None,
) -> None:
    """Migrate a single monolithic temporal HNSW collection to quarterly shards.

    Args:
        collection_path: Monolithic collection directory (e.g. index/code-indexer-temporal-X/).
        index_path: Parent index directory.
        progress_callback: Optional callable(str) for progress reporting.
        repo_path: Optional git repository root for FALLBACK timestamp lookup via
            git log — only consulted for points whose JSON payload lacks
            commit_timestamp (Bug #1286: payload is now the primary, immutable
            source; git history is mutable and not authoritative over it).
            When provided, commit SHAs are extracted from all point_ids and their
            timestamps are fetched from git in one batch call before bucketing,
            so the fallback is ready without a second pass.
    """
    import hnswlib

    collection_name = collection_path.name
    vector_dim, space = _get_vector_dim_and_space(collection_path)

    monolithic_index = hnswlib.Index(space=space, dim=vector_dim)
    monolithic_index.load_index(
        str(collection_path / "hnsw_index.bin"),
        max_elements=_MAX_MONOLITHIC_ELEMENTS,
    )

    try:
        label_to_point_id = _load_id_mapping_from_meta(collection_path)
        point_id_to_rel_path = _load_id_index_bin(collection_path)

        # Build sha_timestamps: extract all unique SHAs from point_ids, then
        # batch-fetch their timestamps from git (one subprocess call).
        sha_timestamps: Dict[str, datetime] = {}
        if repo_path is not None:
            unique_shas: Set[str] = set()
            for pid in label_to_point_id.values():
                sha = _extract_sha_from_point_id(pid)
                if sha:
                    unique_shas.add(sha)
            if unique_shas:
                sha_timestamps = _batch_get_commit_timestamps(repo_path, unique_shas)
                logger.debug(
                    "Migration: fetched %d/%d SHA timestamps from git for %s",
                    len(sha_timestamps),
                    len(unique_shas),
                    collection_name,
                )

        quarter_buckets, drop_counts = _build_quarter_buckets(
            collection_path,
            label_to_point_id,
            point_id_to_rel_path,
            sha_timestamps=sha_timestamps,
        )

        total_shards = len(quarter_buckets)
        total_vectors = sum(len(v) for v in quarter_buckets.values())

        structural_orphans = (
            drop_counts["missing_id_index"] + drop_counts["missing_json"]
        )
        timestamp_unresolved = drop_counts["timestamp_unresolved"]
        total_dropped = structural_orphans + timestamp_unresolved

        # Bug #1286 (supersedes the #1238 "warn-and-continue" policy for structural
        # orphans): losslessness is all-or-nothing. There is no "recoverable" case for
        # a point that cannot be matched — production data confirmed that "proceed with
        # a WARNING" silently discarded tens of thousands of already-embedded vectors
        # while still writing migration_complete.marker and deleting the monolith. ANY
        # drop reason (structural orphan OR unresolved timestamp) now hard-aborts the
        # whole migration BEFORE any shard is built, so the monolith is untouched and
        # the run is retryable after investigation.
        if total_dropped > 0:
            reasons = []
            if drop_counts["missing_id_index"]:
                reasons.append(f"missing_id_index={drop_counts['missing_id_index']}")
            if drop_counts["missing_json"]:
                reasons.append(f"missing_json={drop_counts['missing_json']}")
            if timestamp_unresolved:
                reasons.append(
                    f"timestamp_unresolved={timestamp_unresolved} (unresolved commit "
                    f"timestamps: git lookup returned no results and JSON payloads "
                    f"contain no commit_timestamp)"
                )
            raise RuntimeError(
                f"Migration aborted for {collection_name}: {total_dropped} of "
                f"{len(label_to_point_id)} vectors could not be matched to a quarterly "
                f"shard ({'; '.join(reasons)}). Bug #1286: structural orphans and "
                f"unresolved timestamps are both hard failures now — no silent skips. "
                f"Monolithic index preserved untouched — investigate and re-run."
            )

        vectors_migrated = 0

        for shard_idx, q_str in enumerate(sorted(quarter_buckets.keys())):
            entries = quarter_buckets[q_str]
            shard_name = f"{collection_name}-{q_str}"

            _build_one_shard(
                collection_path=collection_path,
                index_path=index_path,
                shard_name=shard_name,
                entries=entries,
                monolithic_index=monolithic_index,
                vector_dim=vector_dim,
                space=space,
            )

            vectors_migrated += len(entries)

            if progress_callback:
                progress_callback(
                    f"Migrating collection {collection_name}: "
                    f"{shard_idx + 1}/{total_shards} shards, "
                    f"{vectors_migrated}/{total_vectors} vectors"
                )

        # Bug #1286 defect 2: explicit, independent invariant gate BEFORE the
        # marker is written or the monolith is touched — see
        # _verify_migration_lossless_and_complete docstring.
        _verify_migration_lossless_and_complete(
            collection_name=collection_name,
            index_path=index_path,
            label_to_point_id=label_to_point_id,
            quarter_buckets=quarter_buckets,
            vectors_migrated=vectors_migrated,
        )

        _cleanup_monolithic_collection(collection_path)

        logger.info(
            "Migration complete for %s: %d vectors into %d quarterly shards",
            collection_name,
            total_vectors,
            total_shards,
        )

    finally:
        # AC9 constraint: explicit memory free regardless of success or failure.
        del monolithic_index
        gc.collect()


def run_temporal_migration(
    index_path: Path,
    repo_alias: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    repo_path: Optional[Path] = None,
) -> None:
    """Run temporal index migration for a single repo's index directory.

    Discovers all unsharded temporal collections in index_path and migrates
    them one at a time (sequential, not parallel).

    AC4: At job start, scans for .migrating dirs and cleans them up:
    - If matching completed shard exists: remove .migrating remnant
    - If no completed shard: remove .migrating so it is rebuilt from scratch

    Args:
        index_path: Path to the .code-indexer/index/ directory.
        repo_alias: Repository alias (used only for logging).
        progress_callback: Optional callable(str) for progress reporting.
        repo_path: Optional git repository root used for commit timestamp lookup
            via ``git log``.  When None, derived as ``index_path.parent.parent``
            (standard layout: {repo_root}/.code-indexer/index/).  If the
            derived path is not a git repo, git lookup fails gracefully and the
            JSON payload fallback is used automatically.
    """
    if not index_path or not index_path.exists():
        logger.warning(
            "Migration: index_path %s does not exist for repo %s",
            index_path,
            repo_alias,
        )
        return

    # Derive repo_path from index_path when not explicitly provided.
    # Standard layout: {repo_root}/.code-indexer/index/
    effective_repo_path: Path = (
        repo_path if repo_path is not None else index_path.parent.parent
    )

    # AC4: Clean up stale .migrating dirs from previous incomplete runs.
    for entry in list(index_path.iterdir()):
        if not entry.name.endswith(MIGRATING_SUFFIX):
            continue
        shard_name = entry.name[: -len(MIGRATING_SUFFIX)]
        final_shard = index_path / shard_name
        if (final_shard / "collection_meta.json").exists():
            logger.info(
                "Migration: removing stale %s (shard %s already complete)",
                entry.name,
                shard_name,
            )
        else:
            logger.info(
                "Migration: removing incomplete %s (will be rebuilt)", entry.name
            )
        shutil.rmtree(entry)

    # Discover all unsharded temporal collections needing migration.
    for entry in sorted(index_path.iterdir()):
        if not entry.is_dir():
            continue
        if not is_temporal_collection(entry.name):
            continue
        if is_sharded_temporal_collection(entry.name):
            continue
        if (entry / MIGRATION_COMPLETE_MARKER).exists():
            logger.debug(
                "Migration: %s already migrated (marker present) — skipping",
                entry.name,
            )
            continue
        if not (entry / "hnsw_index.bin").exists():
            continue

        logger.info(
            "Migration: starting migration of %s for repo %s",
            entry.name,
            repo_alias,
        )
        _migrate_one_collection(
            collection_path=entry,
            index_path=index_path,
            progress_callback=progress_callback,
            repo_path=effective_repo_path,
        )


def submit_temporal_migration_jobs(
    background_job_manager: Any,
    repos: List[Dict[str, str]],
) -> None:
    """Submit background temporal migration jobs for repos that need migration.

    Called from lifespan startup after BGM is initialized. Non-fatal: logs
    WARNING on scan failure and continues to the next repo.

    AC2: Skip submission if a migration job for the same repo is already
    PENDING or RUNNING (BGM raises DuplicateJobError) — log at DEBUG.

    Args:
        background_job_manager: BackgroundJobManager instance.
        repos: List of repo dicts with keys 'alias' and 'clone_path'.
    """
    from code_indexer.server.repositories.background_jobs import DuplicateJobError

    for repo in repos:
        alias = repo.get("alias", "unknown")
        clone_path = repo.get("clone_path", "")

        if not clone_path:
            continue

        index_path = Path(clone_path) / ".code-indexer" / "index"

        try:
            needs_migration = _needs_temporal_migration(index_path)
        except Exception as exc:
            logger.warning(
                "Migration: failed to scan %s for repo %s: %s",
                index_path,
                alias,
                exc,
            )
            continue

        if not needs_migration:
            continue

        logger.info(
            "Migration: submitting temporal_index_migration job for repo %s", alias
        )

        def _make_migration_fn(path: Path, repo_alias: str, git_root: Path) -> Callable:
            def migration_fn(progress_callback: Optional[Callable] = None) -> Dict:
                run_temporal_migration(
                    index_path=path,
                    repo_alias=repo_alias,
                    progress_callback=progress_callback,
                    repo_path=git_root,
                )
                return {"status": "completed", "repo_alias": repo_alias}

            return migration_fn

        try:
            background_job_manager.submit_job(  # type: ignore[union-attr]
                operation_type="temporal_index_migration",
                func=_make_migration_fn(index_path, alias, Path(clone_path)),
                submitter_username="system",
                is_admin=True,
                repo_alias=alias,
            )
        except DuplicateJobError:
            logger.debug(
                "Migration: temporal_index_migration already running for repo %s — skipping",
                alias,
            )
        except Exception as exc:
            logger.warning(
                "Migration: failed to submit migration job for repo %s: %s",
                alias,
                exc,
            )
