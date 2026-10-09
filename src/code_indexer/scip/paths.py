"""Where a repository's SCIP databases live.

One answer for every SCIP producer and reader: under the repository's index
root (``code_indexer.utils.index_store``), in ``scip/``. Asking here instead of
joining ``.code-indexer/scip`` onto a repo path is what lets the index root move
behind the IndexStore seam without touching each SCIP call site again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from ..utils.index_store import index_root

#: The SCIP directory's name inside an index root.
SCIP_DIRNAME = "scip"

#: The consolidated SCIP database's file name inside the SCIP directory.
SCIP_DB_FILENAME = "index.scip.db"


def scip_index_dir(repo_root: Union[str, Path]) -> Path:
    """The directory holding ``repo_root``'s SCIP databases."""
    return index_root(repo_root) / SCIP_DIRNAME
