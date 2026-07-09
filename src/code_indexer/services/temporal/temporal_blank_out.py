"""Temporal blank-out: hard-delete legacy/version<2 collections (Story #1290 AC19/AC20).

The OLD Cohere temporal index shares the `embed_v4_0` model slug with the NEW
per-commit Cohere index, so slug alone cannot discriminate legacy from new --
the v2 `temporal_structure.json` marker (not the collection name) is the
discriminator. Blank-out enumerates every temporal collection under the
index path and hard-deletes any collection whose marker is MISSING or whose
version is < 2, BEFORE any read, reconcile, or write of that collection.

Deleting the collection's ENTIRE directory in one operation removes
`temporal_progress.json`/`meta.json` along with the vector data -- there is
no partial-file cleanup step to forget.

Callers (the refresh path) are responsible for running this under the
single-writer refresh lock / SharedJobSentinel so concurrent cluster nodes
never race on the same delete (AC20) -- this module provides the pure
enumerate+delete logic; it does not itself acquire any lock.
"""

import logging
from pathlib import Path
from shutil import rmtree
from typing import List

from .temporal_collection_naming import get_temporal_collections
from .temporal_structure_marker import is_v2_structure

logger = logging.getLogger(__name__)


def blank_out_legacy_temporal_collections(index_path: Path) -> List[str]:
    """Hard-delete every temporal collection under `index_path` that is not v2.

    Args:
        index_path: Directory containing collection subdirectories (e.g.
            `.code-indexer/index/`).

    Returns:
        Names of the collections that were deleted (empty list when nothing
        needed deleting -- including when `index_path` does not exist, which
        is a no-op, not an error: there is nothing to blank out yet).

    Raises:
        OSError: If a deletion genuinely fails (permissions, disk error) --
            fails loud rather than silently reporting success (Messi #13
            Anti-Silent-Failure). A failed blank-out must not proceed to
            read/reconcile/write a collection it could not actually clear.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return []

    deleted: List[str] = []
    for name, coll_path in get_temporal_collections(config=None, index_path=index_path):
        if is_v2_structure(coll_path):
            continue
        logger.info(
            "Blank-out: hard-deleting legacy/version<2 temporal collection %s at %s",
            name,
            coll_path,
        )
        rmtree(coll_path)
        deleted.append(name)

    return deleted
