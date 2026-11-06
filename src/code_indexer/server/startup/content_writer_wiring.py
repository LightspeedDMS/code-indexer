"""
Content writer wiring for CIDX server startup.

The write side of ``content_backend_wiring``. Selected by the SAME
``content_backend`` field, because the two cannot disagree: when reads come
from the service that owns the repository files, writes must go there too.

- ``local``     : no writer. The write-mode file tools write this container's
                  disk exactly as they do today, and no plugin is imported.
- ``delegated`` : the plugin's writer. No silent fallback: a pod configured
                  for delegation may not mount the repositories, so writing
                  its local disk would "succeed" where nothing reads it back.
- anything else : raises (as the content backend wiring does).

Callers that write a repository ask :func:`content_writer_for` with the
content source they read it through, so the writer always matches the
reader: the local mode's writer (``cidx-backend-workspace``) for a source
whose implementation reads this pod's disk, the parked delegated writer for
any other.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from code_indexer.server.startup.content_backend_wiring import (
    VALID_CONTENT_BACKENDS,
)

logger = logging.getLogger(__name__)


def build_content_writer(config: Any, *, writer: Optional[Any] = None) -> Optional[Any]:
    """The writer configured by *config* (read via ``getattr``), or None for local."""
    backend_type = getattr(config, "content_backend", "local") or "local"
    if backend_type not in VALID_CONTENT_BACKENDS:
        raise RuntimeError(
            f"Unknown content_backend {backend_type!r}. "
            f"Valid values are {VALID_CONTENT_BACKENDS}."
        )
    if backend_type != "delegated":
        return None
    if writer is None:
        raise RuntimeError(
            "content_backend='delegated' selected but no content writer is "
            "available. Install a plugin registering a factory on the "
            "code_indexer.content_writer entry point, or set "
            "CIDX_CONTENT_WRITER='package.module:factory'. Refusing to fall "
            "back to the local filesystem: a pod built for delegation may not "
            "mount the repositories, and a write there would never be read."
        )
    logger.info("ContentWriter: using delegated writer (%s)", type(writer).__name__)
    return writer


def build_configured_content_writer(config: Any) -> Optional[Any]:
    """Resolve the writer plugin (only when delegated) and build the selection."""
    backend_type = getattr(config, "content_backend", "local") or "local"
    writer = None
    if backend_type == "delegated":
        # Imported lazily so the local default never imports the plugin seam.
        from code_indexer.server.services.content_writer_plugin import (
            resolve_content_writer,
        )

        writer = resolve_content_writer()
    return build_content_writer(config, writer=writer)


def server_content_writer() -> Optional[Any]:
    """The writer the running server parked at startup, or None.

    None outside a server process (CLI, tests) and under the local default.
    Read without importing the app, so a CLI process never constructs it.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    return getattr(_running_server_app_state(), "content_writer", None)


class ContentWriterUnavailableError(RuntimeError):
    """A repository behind a delegated content backend, and no writer parked."""


_local_writer_lock = threading.Lock()
_local_writer: List[Any] = []


def _local_content_writer() -> Any:
    """The content writer seam's ``local`` implementation, resolved once per
    process (raises, naming the seam, when nothing provides it)."""
    with _local_writer_lock:
        if not _local_writer:
            from code_indexer.server.services.content_writer_plugin import (
                resolve_content_writer_for,
            )

            _local_writer.append(resolve_content_writer_for("local"))
        return _local_writer[0]


def content_writer_for(source: Any) -> Any:
    """The writer that writes where the content *source* is read.

    The implementation decides (``source.backend.is_local``): this pod's disk
    is written by the local mode's writer; a delegated backend's repository by
    the writer the running server parked at startup. With none parked this
    raises -- writing this pod's disk instead would "succeed" where nothing
    reads it back.
    """
    if source.backend.is_local:
        return _local_content_writer()
    writer = server_content_writer()
    if writer is None:
        raise ContentWriterUnavailableError(
            "Repository files are not on this server (delegated content backend) "
            "and no content writer is configured; refusing to write the local disk."
        )
    return writer


def working_tree_writer(repo_path: Any) -> Optional[Any]:
    """The writer for the working tree at *repo_path* when it is NOT a
    directory on this pod, else None (write this pod's disk, as always).

    Asked of the content seam's implementation for that path
    (``pod_directory``), never of a server-wide "delegated" flag: a pod-local
    repository is this pod's disk even under a delegated backend. Raises
    :class:`ContentWriterUnavailableError` when the tree is elsewhere and no
    writer is parked.
    """
    from code_indexer.server.services.file_service import (
        content_seam_source,
        pod_directory,
    )

    source = content_seam_source(str(repo_path), consumer="working tree writes")
    if pod_directory(source) is not None:
        return None
    return content_writer_for(source)
