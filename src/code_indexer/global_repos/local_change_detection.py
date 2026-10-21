"""Change detection for a non-git local repository (``local://``).

Such a repository is refreshed only on a trigger, and the refresh compares
the live source tree with the published snapshot to decide whether to
re-index. The comparison must see every completed write the published index
does not hold:

* a file written after the published run listed the repository: its mtime
  is at or after that run's listing-start watermark, which the run records
  as ``last_index_timestamp`` in its indexing metadata (copied into the
  snapshot). The snapshot's own name is taken after indexing, so a file
  written during the run is older than the name but was never indexed;
* a file deleted since the published snapshot: no remaining mtime moves,
  so it is detected by its path missing from the source.

Hidden files and directories (leading ``.``) are not repository content.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator, Set, Tuple

logger = logging.getLogger(__name__)

INDEX_METADATA_RELATIVE = Path(".code-indexer") / "metadata.json"


def iter_visible_files(root: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(relative_path, absolute_path)`` of every non-hidden file."""
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            absolute = os.path.join(current, name)
            yield os.path.relpath(absolute, root), absolute


def published_listing_watermark(snapshot_path: str, snapshot_ts: int) -> float:
    """The earliest moment a write could have been missed by the published
    index: its run's listing-start watermark, never later than the
    snapshot's name. A snapshot whose metadata records no watermark is
    bounded by its name."""
    meta_path = Path(snapshot_path) / INDEX_METADATA_RELATIVE
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return float(snapshot_ts)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Cannot read indexing metadata of snapshot %s (%s: %s); "
            "treating every file as written after its listing",
            snapshot_path,
            type(exc).__name__,
            exc,
        )
        return float("-inf")
    watermark = raw.get("last_index_timestamp") if isinstance(raw, dict) else None
    if isinstance(watermark, bool) or not isinstance(watermark, (int, float)):
        return float(snapshot_ts)
    if watermark <= 0:
        return float(snapshot_ts)
    return min(float(watermark), float(snapshot_ts))


def visible_paths(root: str) -> Set[str]:
    """Relative paths of every non-hidden file under *root*."""
    return {relative for relative, _ in iter_visible_files(root)}
