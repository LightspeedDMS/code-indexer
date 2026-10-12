"""
Pluggable content writer: where golden-repo files are written on a mount-less pod.

The fifth seam to use ``backend_plugin_loader``, after git, content, ripgrep
and index artifacts. Under ``content_backend='delegated'`` the repository is not
on this pod, so the write-mode file tools (create, edit, delete) write through
the service that owns the files (workspace-mcp's ``POST /content/write``); this
module resolves the client for it. It is only ever resolved when the content
backend is delegated, so a local deployment never imports a plugin.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object with
``put(path, data, expected_blob_sha=UNSET) -> new_blob_sha`` and
``delete(path, expected_blob_sha=UNSET) -> bool`` (False when already absent),
over ABSOLUTE paths. ``expected_blob_sha`` is a compare-and-swap on git's blob
sha: leave it out for an unconditional write, ``None`` for "must not exist", a
sha for "must currently be exactly this blob". Failures raise an exception
carrying a string ``code`` (ECONFLICT, EINVAL, EFORBIDDEN, EUNAVAILABLE,
ETRANSPORT, ETOOLARGE, ESECRET, EIO).

Selection (first match wins)
----------------------------
1. ``CIDX_CONTENT_WRITER`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.content_writer`` group.
3. Nothing -- startup refuses a delegated content backend without one.

Misconfiguration fails LOUD (``ContentWriterPluginError``): a silent fallback
would write the local disk of a pod that holds no repository, where nothing
reads the files back.
"""

from __future__ import annotations

from typing import Any, Optional

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_CONTENT_WRITER"
ENTRY_POINT_GROUP = "code_indexer.content_writer"


class ContentWriterPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=ContentWriterPluginError,
    seam_name="content writer",
)


def resolve_content_writer() -> Optional[Any]:
    """Instantiate the configured content writer, or None if none is."""
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
