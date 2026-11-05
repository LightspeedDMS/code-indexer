"""The running ``scip`` feature's process wiring.

``on_startup`` (``cidx_scip.server.lifecycle_hooks``) installs, and
``on_shutdown`` removes, what the MCP handlers, REST routes, the web query
page and the index builder read here:

* the feature's :class:`cidx_sdk.FeatureContext` -- the ``config`` section
  (query limits, workspace retention, ``scip_backend``), ``storage`` for the
  dependency-installation audit, ``permissions`` for REST callers;
* the dependency-installation audit repository (``audit.SCIPAuditRepository``
  over the ``storage`` capability);
* the self-healing workspace cleanup service.

Slots are process wiring, identical on every node, never request state (the
cluster-aware state rule): builds are tracked in the job tracker, indexes live
under each repository's index root. A process where the feature never started
(the CLI, a library test) finds every slot empty; the config section then
reads as its defaults.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import CONFIG, PERMISSIONS
from cidx_sdk.capability_types import ConfigCapability, PermissionsCapability
from cidx_sdk.slots import CapabilitySlot

from cidx_scip.config import (
    BACKEND,
    DEFAULT_BACKEND,
    WORKSPACE_RETENTION_DAYS,
    default_values,
)

#: The feature's context, from ``on_startup`` to ``on_shutdown``.
CONTEXT: CapabilitySlot[FeatureContext] = CapabilitySlot("scip feature context")
#: The dependency-installation audit repository.
AUDIT_REPOSITORY: CapabilitySlot[Any] = CapabilitySlot("scip audit repository")
#: The self-healing workspace cleanup service.
WORKSPACE_CLEANUP: CapabilitySlot[Any] = CapabilitySlot("scip workspace cleanup")

#: Overrides ``scip_workspace_retention_days`` (kept from before the feature
#: owned the key, so a deployment setting it keeps its value).
RETENTION_ENV_VAR = "CIDX_SCIP_WORKSPACE_RETENTION_DAYS"


def context() -> Optional[FeatureContext]:
    """The started feature's context, or None outside a started feature."""
    return CONTEXT.get()


def _capability(name: str) -> Optional[object]:
    ctx = CONTEXT.get()
    if ctx is None or not ctx.has_capability(name):
        return None
    return ctx.capability(name)


def config_section() -> Dict[str, object]:
    """The ``scip`` config section's current values (the ``config``
    capability); its defaults when the feature is not started.

    Raises whatever the config capability raises (a database outage): the
    caller decides how to degrade.
    """
    values = default_values()
    config = _capability(CONFIG)
    if config is not None:
        values.update(cast(ConfigCapability, config).section())
    return values


def setting(name: str) -> Any:
    """One key of the section (its default when it cannot be read)."""
    try:
        return config_section()[name]
    except Exception:  # a setting read never fails a query
        return default_values()[name]


def scip_backend() -> str:
    """``scip_backend`` (bootstrap): ``local`` or ``delegated-plugin``."""
    value = setting(BACKEND)
    return value if isinstance(value, str) and value else DEFAULT_BACKEND


def backend_config() -> Any:
    """What the build funnel and generator wiring read ``scip_backend``
    from (the attribute the server config carried before)."""
    return SimpleNamespace(scip_backend=scip_backend())


def workspace_retention_days() -> int:
    """Self-healing workspace retention: the environment override, else the
    section's value."""
    raw = os.environ.get(RETENTION_ENV_VAR)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(cast(int, setting(WORKSPACE_RETENTION_DAYS)))


def permissions() -> Optional[PermissionsCapability]:
    """The ``permissions`` capability, or None when the feature is not started."""
    return cast(Optional[PermissionsCapability], _capability(PERMISSIONS))
