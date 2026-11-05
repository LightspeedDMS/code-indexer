"""The ONE module of ``cidx_memory`` that imports the code-indexer core.

The memory feature is meant to import ``cidx_sdk`` and third-party packages
only. The memory file format, the catalog writer and the server pieces it
still needs are not in the SDK yet, so this adapter reaches them in
``code_indexer`` and every other module of the package imports them from
here. When the SDK provides them, this file switches to the SDK and nothing
else changes. The import contract ``memory-feature-no-core``
(``.importlinter``) counts these imports in its ratchet baseline; no other
module of the package may add one.

The distribution does not declare ``code-indexer`` as a dependency (the core
depends on ``cidx-memory``, not the reverse), so until this adapter is gone
``cidx_memory`` runs only where the core is installed, which is everywhere the
core loads it.

=============================  ============================================  ===========================
Name                           Used for                                      Replaced by
=============================  ============================================  ===========================
``memory_io``                  the memory file format: atomic write and      stays core-owned (the
                               delete, read, front matter (the core's        catalog projection reads
                               catalog projection reads the same files)      it); an SDK utility
``cidx_meta_content``          writes to a shared (delegated) catalog        the content writer seam
``capabilities``               ``content_seam_delegated`` and the            ``content_seam`` capability
                               ``SERVER_PLATFORM_HOST`` slot                 and SDK capabilities
``FilesystemVectorStore``      the catalog's ``memories`` collection         an index-store/search
                               (lazy)                                        capability
``request_cidx_meta_refresh``  the catalog refresh after a write (lazy)      a catalog-writes capability
``server_platform()``          the app state (golden repositories            SDK capabilities as each
                               directory, refresh scheduler, debouncer),     is added (see
                               the MCP envelope and the error-log format     ``ServerPlatformHost``)
=============================  ============================================  ===========================

The memory visibility rule is NOT reached here: retrieval asks the
``access_projection`` capability (``server/retrieval.py``).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Dict

from code_indexer.kernel import capabilities as capabilities
from code_indexer.server.services import cidx_meta_content as cidx_meta_content
from code_indexer.server.services.memory_io import (
    MemoryFileNotFoundError as MemoryFileNotFoundError,
    atomic_delete_memory_file as atomic_delete_memory_file,
    atomic_write_memory_file as atomic_write_memory_file,
    read_memory_file as read_memory_file,
)

if TYPE_CHECKING:
    from code_indexer.global_repos.meta_description_hook import (
        request_cidx_meta_refresh as request_cidx_meta_refresh,
    )
    from code_indexer.storage.filesystem_vector_store import (
        FilesystemVectorStore as FilesystemVectorStore,
    )

#: Names resolved on first use, so importing a light module of the feature
#: never loads the vector store or the description hook.
_LAZY_NAMES: Dict[str, str] = {
    "FilesystemVectorStore": "code_indexer.storage.filesystem_vector_store",
    "request_cidx_meta_refresh": "code_indexer.global_repos.meta_description_hook",
}

#: What :func:`server_platform` returns.
ServerPlatformHost = capabilities.ServerPlatformHost


def server_platform() -> ServerPlatformHost:
    """The server's platform pieces (see the table above).

    Raises:
        RuntimeError: no server is loaded in this process (the slot is filled
            when the core's server package is imported).
    """
    host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        raise RuntimeError(
            "the code-indexer server platform is not loaded in this process"
        )
    return host


def content_seam_delegated() -> bool:
    """True when repository files are read through a non-local backend."""
    return bool(capabilities.content_seam_delegated())


def __getattr__(name: str) -> object:
    module_name = _LAZY_NAMES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
