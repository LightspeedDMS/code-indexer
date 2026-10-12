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
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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
