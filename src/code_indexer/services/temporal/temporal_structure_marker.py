"""v2 temporal_structure.json marker (Story #1290 AC8, AC19, AC20, AC27).

The marker -- not the collection slug -- is the discriminator between a
legacy per-file-diff shard and a new per-commit v2 shard, because the OLD
Cohere temporal index shares the `embed_v4_0` slug with the NEW one (Epic
#1289 risk mitigation). It is written at collection CREATE time, BEFORE the
first embed/flush, so a crash mid-index cannot leave a new collection
looking legacy (which would otherwise trigger a spurious blank-out
delete-rebuild loop).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STRUCTURE_MARKER_FILENAME = "temporal_structure.json"
STRUCTURE_VERSION = 2
STRUCTURE_LAYOUT = "per_commit"


def write_structure_marker(collection_dir: Path, model_slug: str) -> None:
    """Atomically write the v2 structure marker into `collection_dir`.

    Uses a tempfile + os.replace so a crash mid-write never leaves a
    partially-written (and therefore corrupt/unreadable) marker on disk --
    read_structure_marker() would see either the old content or the new
    content, never a torn write.

    Args:
        collection_dir: Directory of the temporal shard collection.
        model_slug: Filesystem-safe embedder model slug (e.g. "voyage_context_4").
    """
    collection_dir = Path(collection_dir)
    collection_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "version": STRUCTURE_VERSION,
        "layout": STRUCTURE_LAYOUT,
        "model": model_slug,
    }
    marker_path = collection_dir / STRUCTURE_MARKER_FILENAME
    tmp_path = collection_dir / f".{STRUCTURE_MARKER_FILENAME}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(marker, f)
    os.replace(tmp_path, marker_path)


def read_structure_marker(collection_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the structure marker from `collection_dir`, or None if absent/corrupt.

    A missing or unparseable marker is treated identically (None) by callers
    that decide legacy-vs-v2 status -- both cases mean "not proven v2".
    """
    marker_path = Path(collection_dir) / STRUCTURE_MARKER_FILENAME
    if not marker_path.exists():
        return None
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Corrupt temporal_structure.json at %s: %s -- treating as absent",
            marker_path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_v2_structure(collection_dir: Path) -> bool:
    """Return True iff `collection_dir` carries a marker with version >= 2.

    Used by blank-out (AC19/AC20): missing marker OR version < 2 means the
    collection is legacy and must be hard-deleted before any read, reconcile,
    or write.
    """
    marker = read_structure_marker(collection_dir)
    if marker is None:
        return False
    version = marker.get("version")
    return isinstance(version, int) and version >= STRUCTURE_VERSION
