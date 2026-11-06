"""The index store seam's local mode, resolved for the core's library code.

The local :class:`~cidx_sdk.seams.index_store.IndexStore` is whatever module
registers the entry name ``local`` on ``code_indexer.index_artifacts``
(``cidx-backend-workspace``); the kernel has no implementation of its own, so
with nothing installed this raises
:class:`cidx_sdk.errors.SeamUnavailableError` naming the seam. Library code
(plain CLI indexing, the refresh scheduler's default) asks here; the server's
publishing store is built at startup (``server/startup/index_store_wiring.py``).
"""

from __future__ import annotations

import sys
from importlib.metadata import entry_points

from cidx_sdk.seams.errors import IndexArtifactsPluginError
from cidx_sdk.seams.index_store import IndexStore
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import LOCAL_MODE

from code_indexer.kernel.seam_resolution import module_entry_point_source

#: Kept importable: tests patch ``entry_points`` here to control resolution.
__all__ = ["entry_points", "local_index_store", "reset_cache", "sys"]

_resolver = SeamResolver(
    seam="index_artifacts",
    env_var="CIDX_INDEX_ARTIFACTS",
    error_type=IndexArtifactsPluginError,
    display_name="index store",
    entry_point_source=module_entry_point_source(__name__),
)


def local_index_store() -> IndexStore:
    """A new instance of the index store installed for the ``local`` mode.

    Raises:
        SeamUnavailableError: nothing (or more than one module) provides it.
        IndexArtifactsPluginError: the provider is installed but unusable.
    """
    store: IndexStore = _resolver.resolve(LOCAL_MODE)
    return store


def reset_cache() -> None:
    """Forget the resolved provider. For tests."""
    _resolver.reset_cache()
