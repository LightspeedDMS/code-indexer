"""The running ``depmap`` feature's process wiring.

``on_startup`` (``cidx_depmap.server.lifecycle``) installs, and
``on_shutdown`` removes, the feature's :class:`cidx_sdk.FeatureContext`; the
MCP tools, the web pages and the analysis services read their capabilities
and settings from here. The running :class:`DependencyMapService` is kept in
the feature's own slot (:data:`SERVICE`, installed by ``on_startup`` once the
scheduler runs, emptied by ``on_shutdown``) so every route and tool of the
process finds the one instance; the core never names it. Process wiring,
identical on every node; cross-request state lives in the shared stores the
service already uses (tracking backend, job tracker, sentinel files).

A process where the feature never started (a library test that mounts the
routes directly) finds both slots empty: settings read as their defaults, the
access projection is unavailable (nothing is served), and there is no service
(the tools and pages answer that the dependency map is unavailable).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG
from cidx_sdk.capability_types import AccessProjectionCapability, ConfigCapability
from cidx_sdk.slots import CapabilitySlot

from cidx_depmap.config import OWNED_KEYS, default_values

#: The feature's context, from ``on_startup`` to ``on_shutdown``.
CONTEXT: CapabilitySlot[FeatureContext] = CapabilitySlot("depmap feature context")

#: The running :class:`DependencyMapService`, from a successful service start
#: in ``on_startup`` to ``on_shutdown``; empty when it never started.
SERVICE: CapabilitySlot[Any] = CapabilitySlot("depmap service")


def _capability(name: str) -> Optional[object]:
    ctx = CONTEXT.get()
    if ctx is None or not ctx.has_capability(name):
        return None
    return ctx.capability(name)


def settings() -> Dict[str, Any]:
    """The ``depmap`` config section (the ``config`` capability), every key
    at its stored value or default; the defaults when not started."""
    values: Dict[str, Any] = dict(default_values())
    config = _capability(CONFIG)
    if config is not None:
        values.update(cast(ConfigCapability, config).section())
    return values


def access_projection() -> Optional[AccessProjectionCapability]:
    """The ``access_projection`` capability, or None when not started."""
    projection = _capability(ACCESS_PROJECTION)
    return None if projection is None else cast(AccessProjectionCapability, projection)


def dependency_map_service() -> Any:
    """The running service, or None when it is not started."""
    return SERVICE.get()


class DepMapClaudeConfig:
    """The core's ``claude_integration_config`` as the dependency map reads
    it: the keys this feature owns (:data:`cidx_depmap.config.OWNED_KEYS`)
    come from the ``depmap`` section; every other attribute is the core
    object's. Without the ``config`` capability (a library test that builds
    the service by hand) an owned key is read from the wrapped object when
    it has one, else it is the default.
    """

    def __init__(self, claude_config: Any) -> None:
        self._claude_config = claude_config

    def __getattr__(self, name: str) -> Any:
        if name in OWNED_KEYS:
            if _capability(CONFIG) is not None:
                return settings()[name]
            if hasattr(self._claude_config, name):
                return getattr(self._claude_config, name)
            return default_values()[name]
        return getattr(self._claude_config, name)


class DepMapConfigManager:
    """The core's config service as the dependency map reads it:
    ``get_claude_integration_config()`` answers a :class:`DepMapClaudeConfig`;
    everything else is the config service's."""

    def __init__(self, config_service: Any) -> None:
        self._config_service = config_service

    def get_claude_integration_config(self) -> DepMapClaudeConfig:
        return DepMapClaudeConfig(self._config_service.get_claude_integration_config())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._config_service, name)
