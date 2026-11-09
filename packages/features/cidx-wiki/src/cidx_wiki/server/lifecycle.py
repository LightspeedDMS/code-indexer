"""What the ``wiki`` feature wires at ``on_startup`` and removes at
``on_shutdown``.

* The feature context (``runtime.CONTEXT``), so routes and the MCP tool read
  their capabilities from it.
* The cache invalidator (Bug #1667, Story #304): a :class:`WikiCache` over
  the core's shared ``wiki_cache`` store (PostgreSQL in cluster mode, the
  server SQLite database solo; built on the first event), subscribed to the
  core's
  ``repository_events`` -- file and git writes, write-mode exit, golden
  repository removal and rename.

Process wiring, identical on every node; every invalidation goes to the
shared store.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import REPOSITORY_EVENTS
from cidx_sdk.capability_types import RepositoryEventsCapability

from cidx_wiki._platform import server_platform
from cidx_wiki.server import runtime
from cidx_wiki.server.wiki_cache import WikiCache
from cidx_wiki.server.wiki_cache_invalidator import wiki_cache_invalidator

logger = logging.getLogger(__name__)


class WikiRuntime:
    """The started feature's wiring (one per process)."""

    def __init__(self) -> None:
        self._unsubscribe: Optional[Callable[[], None]] = None

    def start(self, ctx: FeatureContext) -> None:
        runtime.CONTEXT.install(ctx)
        wiki_cache_invalidator.set_wiki_cache_factory(_shared_wiki_cache)
        events = cast(RepositoryEventsCapability, ctx.capability(REPOSITORY_EVENTS))
        self._unsubscribe = events.subscribe(wiki_cache_invalidator)

    def stop(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        wiki_cache_invalidator.set_wiki_cache_factory(None)
        wiki_cache_invalidator.wiki_cache = None
        runtime.CONTEXT.uninstall()


def _shared_wiki_cache() -> Optional[WikiCache]:
    """A WikiCache over the core's shared store, built on the first
    repository event; None while the server has no golden repository manager
    (the event is then a no-op, and the next one tries again)."""
    platform = server_platform()
    grm = platform.app_attr("golden_repo_manager")
    db_path = getattr(grm, "db_path", None) if grm is not None else None
    if not db_path:
        logger.debug("wiki: no golden repository manager yet; event not applied")
        return None
    backend = platform.backend_registry_attr("wiki_cache", "wiki.lifecycle")
    return WikiCache(str(db_path), storage_backend=backend)
