"""What the ``memory`` feature wires at ``on_startup`` and removes at
``on_shutdown``.

* The feature context (``runtime.CONTEXT``): the search section reads its
  settings and the memory visibility rule from it.
* The memory store (Story #877, ``runtime.STORE``): built over the shared
  catalog's ``memories/`` directory, the server's lock directory and the
  core's catalog refresh wiring (the refresh scheduler's cidx-meta write lock
  and the refresh debouncer). Without that wiring (a server whose catalog
  refresh could not start) the store is not built: the tools answer
  ``service_unavailable`` and the routes 503, as before the move.

Process wiring, identical on every node.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from cidx_sdk import FeatureContext

from cidx_memory._platform import server_platform
from cidx_memory.server import runtime
from cidx_memory.server.memory_store_service import MemoryStoreService
from cidx_memory.server.memory_store_service_factory import (
    build_memory_store_service,
)

logger = logging.getLogger(__name__)


class MemoryRuntime:
    """The started feature's wiring (one per process)."""

    def start(self, ctx: FeatureContext) -> None:
        runtime.CONTEXT.install(ctx)
        store = _build_store()
        if store is not None:
            runtime.STORE.install(store)

    def stop(self) -> None:
        runtime.STORE.uninstall()
        runtime.CONTEXT.uninstall()


def _app_state() -> Any:
    app = server_platform().app_attr("app")
    return getattr(app, "state", None) if app is not None else None


def _build_store() -> Optional[MemoryStoreService]:
    state = _app_state()
    golden_repos_dir = getattr(state, "golden_repos_dir", None)
    server_data_dir = getattr(state, "server_data_dir", None)
    debouncer = getattr(state, "cidx_meta_debouncer", None)
    scheduler = server_platform().refresh_scheduler()
    missing = [
        name
        for name, value in (
            ("golden_repos_dir", golden_repos_dir),
            ("server_data_dir", server_data_dir),
            ("cidx_meta_debouncer", debouncer),
            ("refresh_scheduler", scheduler),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "memory store not started: the server has no %s; the memory tools "
            "answer service_unavailable",
            ", ".join(missing),
        )
        return None
    bundle = build_memory_store_service(
        golden_repos_dir=Path(str(golden_repos_dir)),
        server_data_dir=Path(str(server_data_dir)),
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
    )
    logger.info("memory store started (memories_dir=%s)", bundle.memories_dir)
    return bundle.service
