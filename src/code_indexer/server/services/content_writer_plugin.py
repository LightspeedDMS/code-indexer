"""
Pluggable content writer: where golden-repo files are written on a mount-less pod.

The fifth seam to use ``backend_plugin_loader``, after git, content, ripgrep
and index artifacts. Under ``content_backend='delegated'`` the repository is not
on this pod, so the write-mode file tools (create, edit, delete) write through
the service that owns the files (the content service's ``POST /content/write``); this
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

The entry named ``local`` (``cidx-backend-workspace``) is never picked for the
delegated mode; it is reached by mode through :func:`resolve_content_writer_for`.

Misconfiguration fails LOUD (``ContentWriterPluginError``): a silent fallback
would write the local disk of a pod that holds no repository, where nothing
reads the files back.
"""

from __future__ import annotations

from typing import Any, Optional

from cidx_sdk.entry_points import CONTENT_WRITER_ENTRY_POINT_GROUP
from cidx_sdk.seams.content_writer import (
    ECONFLICT as ECONFLICT,
    ETRANSPORT as ETRANSPORT,
    EUNAVAILABLE as EUNAVAILABLE,
)
from cidx_sdk.seams.errors import ContentWriterPluginError as ContentWriterPluginError

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_CONTENT_WRITER"

# The writer codes (ECONFLICT: a failed ``expected_blob_sha`` precondition;
# EUNAVAILABLE: the content service cannot take writes at all; ETRANSPORT: it could
# not be reached or answered something unreadable) are the seam's, from the SDK.
ENTRY_POINT_GROUP = CONTENT_WRITER_ENTRY_POINT_GROUP


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=ContentWriterPluginError,
    seam_name="content writer",
)


def resolve_content_writer() -> Optional[Any]:
    """Instantiate the configured content writer, or None if none is."""
    return _resolver.resolve()


def resolve_content_writer_for(mode: str) -> Any:
    """Instantiate the content writer installed for *mode* (``local``,
    ``delegated``); refuses loudly, naming the seam, when nothing provides it.

    ``local`` is ``cidx-backend-workspace``'s writer (this pod's disk, same
    contract as the delegate). Callers still write the disk themselves under
    the local content backend (``content_writer_wiring`` resolves no writer
    there); routing them through this seam is the content-caller step, which
    lands with the catalog access projection above the seam.
    """
    return _resolver.resolve_mode(mode)


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
