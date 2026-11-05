"""The running ``wiki`` feature's process wiring.

``on_startup`` (``cidx_wiki.server.lifecycle``) installs, and ``on_shutdown``
removes, the feature's :class:`cidx_sdk.FeatureContext`; the routes and the
MCP tool read every SDK capability they use from it (``config``,
``permissions``, ``access_projection``). Process wiring, identical on every
node, never request state (the Cluster-Aware State rule).

A process where the feature never started (a library test that mounts the
wiki's router directly) finds the slot empty: the config section reads as
its defaults, nothing is restricted by the access projection, and no extra
permission is held.
"""

from __future__ import annotations

from typing import Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG, PERMISSIONS
from cidx_sdk.capability_types import (
    AccessProjectionCapability,
    ConfigCapability,
    PermissionsCapability,
)
from cidx_sdk.slots import CapabilitySlot

from cidx_wiki.config import WikiConfig, default_values, wiki_config_from

#: The feature's context, from ``on_startup`` to ``on_shutdown``.
CONTEXT: CapabilitySlot[FeatureContext] = CapabilitySlot("wiki feature context")


def context() -> Optional[FeatureContext]:
    """The started feature's context, or None outside a started feature."""
    return CONTEXT.get()


def _capability(name: str) -> Optional[object]:
    ctx = CONTEXT.get()
    if ctx is None or not ctx.has_capability(name):
        return None
    return ctx.capability(name)


def wiki_config() -> WikiConfig:
    """The ``wiki`` config section as a :class:`WikiConfig` (the ``config``
    capability); the defaults when the feature is not started.

    Raises whatever the config capability raises (a database outage): the
    caller decides how to degrade.
    """
    values = default_values()
    config = _capability(CONFIG)
    if config is not None:
        values.update(cast(ConfigCapability, config).section())
    return wiki_config_from(values)


def permissions() -> Optional[PermissionsCapability]:
    """The ``permissions`` capability, or None when not started."""
    perms = _capability(PERMISSIONS)
    return None if perms is None else cast(PermissionsCapability, perms)


def access_projection() -> Optional[AccessProjectionCapability]:
    """The ``access_projection`` capability, or None when not started."""
    projection = _capability(ACCESS_PROJECTION)
    return None if projection is None else cast(AccessProjectionCapability, projection)
