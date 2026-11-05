"""The running ``memory`` feature's process wiring.

``on_startup`` (``cidx_memory.server.lifecycle``) installs, and
``on_shutdown`` removes, the feature's :class:`cidx_sdk.FeatureContext` and
the memory store; the MCP tools, REST routes and the search section read them
from here. Process wiring, identical on every node, never request state (the
Cluster-Aware State rule): every memory lives in the shared catalog and every
lock is a file lock.

A process where the feature never started (a library test that mounts the
routes directly) finds the slots empty: the config section reads as its
defaults, the store is unavailable (503 / ``service_unavailable``) and no
memory is visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG
from cidx_sdk.capability_types import AccessProjectionCapability, ConfigCapability
from cidx_sdk.slots import CapabilitySlot

from cidx_memory.config import (
    MemoryRetrievalConfig,
    default_values,
    memory_retrieval_config_from,
)

if TYPE_CHECKING:
    from cidx_memory.server.memory_store_service import MemoryStoreService

#: The feature's context, from ``on_startup`` to ``on_shutdown``.
CONTEXT: CapabilitySlot[FeatureContext] = CapabilitySlot("memory feature context")

#: The memory store, from ``on_startup`` to ``on_shutdown`` (None when the
#: server has no catalog refresh wiring to build it on).
STORE: CapabilitySlot["MemoryStoreService"] = CapabilitySlot("memory store service")


def _capability(name: str) -> Optional[object]:
    ctx = CONTEXT.get()
    if ctx is None or not ctx.has_capability(name):
        return None
    return ctx.capability(name)


def memory_retrieval_config() -> MemoryRetrievalConfig:
    """The ``memory`` config section (the ``config`` capability); the
    defaults when the feature is not started.

    Raises whatever the config capability raises (a database outage): the
    caller decides how to degrade.
    """
    values = default_values()
    config = _capability(CONFIG)
    if config is not None:
        values.update(cast(ConfigCapability, config).section())
    return memory_retrieval_config_from(values)


def access_projection() -> Optional[AccessProjectionCapability]:
    """The ``access_projection`` capability, or None when not started."""
    projection = _capability(ACCESS_PROJECTION)
    return None if projection is None else cast(AccessProjectionCapability, projection)


def memory_store() -> Optional["MemoryStoreService"]:
    """The started feature's memory store, or None."""
    return STORE.get()
