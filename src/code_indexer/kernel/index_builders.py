"""The index types features contribute to the indexing pipeline.

The core's golden-repository pipeline (``global_repos.refresh_scheduler``,
the golden repo manager's add-index job) and the activated-repository
reindex build the core's own index types themselves. Any other type -- SCIP,
from the ``scip`` feature -- is built by the enabled feature that
contributes it (``Feature.index_builders()``, SDK 0.5), looked up here by
type. With no builder for a type the pipeline builds nothing for it.

Process wiring, identical on every node (it follows ``enabled_features``):
the server's feature wiring replaces the whole set whenever an app is built
(``server/features/index_builders.py``). Lives in the kernel because the
library pipeline (``global_repos``) reads it and must not import the server.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Sequence, Tuple

from cidx_sdk import IndexBuilderContribution

#: The pipeline's name for the SCIP index (the registry's ``enable_scip`` flag,
#: the ``index_types`` value of an add-index request); its builder is the
#: ``scip`` feature's.
SCIP_INDEX_TYPE = "scip"

_lock = threading.Lock()
_builders: Dict[str, IndexBuilderContribution] = {}


def install_index_builders(builders: Sequence[IndexBuilderContribution]) -> None:
    """Replace the installed builders with *builders* (unique types)."""
    by_type = {builder.index_type: builder for builder in builders}
    with _lock:
        _builders.clear()
        _builders.update(by_type)


def index_builder(index_type: str) -> Optional[IndexBuilderContribution]:
    """The builder of *index_type*, or None when no enabled feature builds it."""
    with _lock:
        return _builders.get(index_type)


def contributed_index_types() -> Tuple[str, ...]:
    """Every index type an enabled feature builds, sorted."""
    with _lock:
        return tuple(sorted(_builders))
