"""
Pluggable index-artifact client: the service that holds published indexes.

The fourth seam to use ``backend_plugin_loader``, after git, content and
ripgrep. Under ``index_store_backend='delegated'`` a built index is published
to, and checked out from, an artifact service (workspace-mcp's index store);
this module resolves the client for it. With no plugin configured nothing is
returned and the caller keeps the local store, so an existing deployment is
unaffected.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object with
``current_version(alias)``, ``publish(alias, base_version, source_dir)`` and
``checkout(alias, dest_dir, version=None)`` (see ``DelegatedIndexStore``).

Selection (first match wins)
----------------------------
1. ``CIDX_INDEX_ARTIFACTS`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.index_artifacts`` group.
3. Nothing -- the caller uses the local store.

Misconfiguration fails LOUD (``IndexArtifactsPluginError``): a pod without the
repository volume keeps its built index on scratch space, and a silent fallback
to the local store would never publish it anywhere a restart could recover.
"""

from __future__ import annotations

from typing import Any, Optional

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_INDEX_ARTIFACTS"
ENTRY_POINT_GROUP = "code_indexer.index_artifacts"


class IndexArtifactsPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=IndexArtifactsPluginError,
    seam_name="index artifacts",
)


def resolve_index_artifacts() -> Optional[Any]:
    """Instantiate the configured index-artifact client, or None if none is."""
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
