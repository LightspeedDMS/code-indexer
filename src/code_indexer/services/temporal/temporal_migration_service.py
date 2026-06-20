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
import os
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from code_indexer.services.temporal.temporal_collection_naming import (
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
        if (entry / MIGRATION_COMPLETE_MARKER).exists():
            continue
        if (entry / "hnsw_index.bin").exists():
            return True
    return False


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
) -> None:
    """Write collection_meta.json for a shard directory. Atomic via temp+rename."""
    hnsw_file = shard_dir / "hnsw_index.bin"
    file_size = hnsw_file.stat().st_size if hnsw_file.exists() else 0
    meta = {
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
) -> Dict[str, List[Tuple[int, str, Path]]]:
    """Group vectors into quarterly buckets based on commit_timestamp.

    Returns:
        Dict mapping quarter_str (e.g. "2024Q3") to list of
        (label, point_id, src_json_path) tuples.
    """
    buckets: Dict[str, List[Tuple[int, str, Path]]] = {}

    for label, point_id in label_to_point_id.items():
        rel_path = point_id_to_rel_path.get(point_id)
        if rel_path is None:
            logger.warning(
                "Migration: point_id %s has no id_index entry in %s — skipping",
                point_id,
                collection_path.name,
            )
            continue

        src_json = collection_path / rel_path
        if not src_json.exists():
            logger.warning(
                "Migration: JSON payload %s not found — skipping point %s",
                src_json,
                point_id,
            )
            continue

        try:
            with open(src_json) as f:
                payload_data = json.load(f)
            commit_ts = payload_data.get("payload", {}).get("commit_timestamp")
            if commit_ts is None:
                commit_ts = payload_data.get("commit_timestamp")
            if commit_ts is None:
                logger.warning(
                    "Migration: no commit_timestamp in %s — skipping", src_json
                )
                continue
            dt = datetime.fromtimestamp(int(commit_ts), tz=timezone.utc)
            q = quarter_suffix(dt)
        except Exception as exc:
            logger.warning("Migration: error reading %s: %s — skipping", src_json, exc)
            continue

        buckets.setdefault(q, []).append((label, point_id, src_json))

    return buckets


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
    _write_collection_meta(
        shard_dir=migrating_dir,
        shard_name=shard_name,
        vector_dim=vector_dim,
        space=space,
        id_mapping=new_id_mapping,
    )

    # Atomic rename to final shard dir.
    os.replace(migrating_dir, final_shard_dir)


def _cleanup_monolithic_collection(collection_path: Path) -> None:
    """Write migration marker; delete monolithic binaries and JSON payload files."""
    # Step 6: Write migration_complete.marker.
    (collection_path / MIGRATION_COMPLETE_MARKER).write_text("migration complete\n")

    # Step 7: Delete monolithic hnsw_index.bin and id_index.bin.
    for fname in ("hnsw_index.bin", "id_index.bin"):
        p = collection_path / fname
        if p.exists():
            p.unlink()

    # Step 8: Delete monolithic JSON payload files.
    for json_file in list(collection_path.rglob("*.json")):
        if json_file.name == "collection_meta.json":
            continue
        try:
            json_file.unlink()
        except OSError as exc:
            logger.warning("Migration: failed to delete %s: %s", json_file, exc)


def _migrate_one_collection(
    collection_path: Path,
    index_path: Path,
    progress_callback: Optional[Callable[[str], None]],
) -> None:
    """Migrate a single monolithic temporal HNSW collection to quarterly shards.

    Args:
        collection_path: Monolithic collection directory (e.g. index/code-indexer-temporal-X/).
        index_path: Parent index directory.
        progress_callback: Optional callable(str) for progress reporting.
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

        quarter_buckets = _build_quarter_buckets(
            collection_path, label_to_point_id, point_id_to_rel_path
        )

        total_shards = len(quarter_buckets)
        total_vectors = sum(len(v) for v in quarter_buckets.values())
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
    """
    if not index_path or not index_path.exists():
        logger.warning(
            "Migration: index_path %s does not exist for repo %s",
            index_path,
            repo_alias,
        )
        return

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

        def _make_migration_fn(path: Path, repo_alias: str) -> Callable:
            def migration_fn(progress_callback: Optional[Callable] = None) -> Dict:
                run_temporal_migration(
                    index_path=path,
                    repo_alias=repo_alias,
                    progress_callback=progress_callback,
                )
                return {"status": "completed", "repo_alias": repo_alias}

            return migration_fn

        try:
            background_job_manager.submit_job(  # type: ignore[union-attr]
                operation_type="temporal_index_migration",
                func=_make_migration_fn(index_path, alias),
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
