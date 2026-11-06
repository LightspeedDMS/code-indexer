"""
Pluggable search backend: let a deployment supply the delegated ``SearchBackend``.

The search sibling of ``content_backend_plugin`` and ``ripgrep_backend_plugin``.
The ``SearchBackend`` seam decides WHERE FTS and semantic queries execute:
``local`` (the default) reads this container's Tantivy/HNSW indexes,
``delegated`` forwards the query to the service that owns the indexes, so cidx
needs neither the index files nor the embedder.

``build_search_backend(config, *, delegated_backend=...)`` needs something to
inject for the delegated slot. This module resolves it. With no plugin
configured nothing is returned and the caller keeps the ``local`` default, so an
existing deployment is unaffected.

The contract
------------
A plugin is a zero-argument FACTORY callable::

    def make_search_backend() -> SearchBackend:
        ...

returning any object satisfying the ``SearchBackend`` protocol (``search``).
Endpoint, token and timeout come from the plugin's own environment.

Selection (first match wins)
----------------------------
1. ``CIDX_SEARCH_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.search_backend`` group.
3. Nothing -- the caller uses the built-in local index path.

Misconfiguration fails LOUD (``SearchBackendPluginError``) rather than silently
returning None: a stateless EKS pod configured for delegation has no local
Tantivy/HNSW indexes at all, and a silent fallback would surface as "index not
available" errors rather than a configuration problem.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cidx_sdk.entry_points import SEARCH_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.errors import SearchBackendPluginError as SearchBackendPluginError

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_SEARCH_BACKEND"
ENTRY_POINT_GROUP = SEARCH_BACKEND_ENTRY_POINT_GROUP

# () -> SearchBackend
SearchBackendFactoryCallable = Callable[[], Any]


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=SearchBackendPluginError,
    seam_name="search",
)


def get_search_backend_factory() -> Optional[SearchBackendFactoryCallable]:
    """Return the configured search-backend factory, or None for local.

    Raises:
        SearchBackendPluginError: a plugin was configured but is unusable.
            Raised rather than silently returning None -- see module docstring.
    """
    return _resolver.get_factory()


def resolve_delegated_search_backend() -> Optional[Any]:
    """Instantiate the delegated SearchBackend, or None if none is configured.

    A configured-but-broken factory still raises -- fail loud, never a silent
    fallback to reading local index files.
    """
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
