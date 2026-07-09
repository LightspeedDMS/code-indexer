"""Projection-matrix self-heal helper for temporal quarterly shards.

Story #1290 AC18: relocated (not deleted) from the now-removed
temporal_migration_service.py. Used by two call sites that must keep
resolving:
  - storage/filesystem_vector_store.py (Bug #1264 write-chokepoint self-heal)
  - services/temporal/temporal_indexer.py (Bug #1242 shard-prep-loop self-heal)

Ensures a quarterly shard directory has a projection_matrix.npy, copying it
from a source (base/monolith) collection when available or regenerating a
fresh one otherwise. Both branches write atomically (temp file + os.replace)
so two callers healing the same shard concurrently never observe a torn
file.
"""

import json
import logging
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


def _atomic_replace_via_tmp(final_path: Path, write_fn: Callable[[Path], None]) -> None:
    """Write final_path atomically: write_fn populates a uniquely-named temp
    file in the SAME directory, then os.replace() renames it onto final_path.

    Bug #1264: closes the torn-read window where a concurrent reader/healer
    of the same target path could observe a partially-written file.
    os.replace() is atomic on POSIX and overwrites the destination, so any
    concurrent observer sees either the old (absent) state or the fully
    written new file -- never something in between. The temp filename is
    unique per call (uuid4) so two concurrent callers writing the same
    final_path never collide on the SAME temp file either.

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


def _read_monolith_quantization_range(
    collection_path: Path, vector_dim: int
) -> Dict[str, float]:
    """Read quantization_range from a source collection's collection_meta.json.

    Falls back to the create_collection formula (±3·sqrt(64/input_dim)) when
    the source meta is absent, corrupt, or missing the key.

    Args:
        collection_path: Source (base/monolith) collection directory.
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


def _ensure_shard_has_projection_matrix(
    shard_path: Path,
    source_collection_path: Optional[Path],
    vector_dim: int,
) -> None:
    """Ensure shard_path has projection_matrix.npy (copy from source or regenerate).

    Idempotent — returns immediately when the matrix file is already present.

    Preferred: copies from source_collection_path (base collection) so the
    write-path bucket layout (vector @ matrix -> hex dir) is consistent with
    any vectors previously written under that source. Falls back to
    regenerating a fresh matrix when the source is None or lacks the file.

    Both branches write atomically (temp file in the same directory, then
    os.replace()) so two callers healing the same shard concurrently never
    produce or observe a torn/partial projection_matrix.npy (Bug #1264).

    Also backfills quantization_range in the shard's collection_meta.json when
    the key is missing (both copy and regenerate paths).

    Args:
        shard_path: Shard (or in-progress) directory.
        source_collection_path: Base collection to copy from, or None.
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
        matrix: Any = manager.create_projection_matrix(
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
