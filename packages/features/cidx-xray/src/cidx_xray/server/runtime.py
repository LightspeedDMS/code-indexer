"""The running ``xray`` feature's process wiring.

``on_startup`` (``cidx_xray.server.lifecycle``) installs, and ``on_shutdown``
removes, four things the MCP handlers and REST routes read here:

* the feature's :class:`cidx_sdk.FeatureContext` -- every SDK capability the
  handlers use comes from it (``config``, ``background_jobs``, ``permissions``,
  ``storage``, ``telemetry``);
* the dedicated X-Ray thread pool (Bug #1070: isolated from the job manager's
  pool);
* the X-Ray cell limiter (all scan executions share its slots, sized by the
  ``xray_worker_threads`` setting);
* the execution backend ``xray_backend`` selected (none under ``local``),
  which the placement host (``backend_wiring.FeaturePlacementHost``) hands
  every ``RustNativeBackend``.

Slots are process wiring, identical on every node, never request state (the
cluster-aware state rule): results, jobs and pages live in the job tracker
and the payload cache. A process where the feature never started (the CLI, a
library test) finds every slot empty; the config section then reads as its
defaults, as the server's own settings read outside a running server.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import (
    BACKGROUND_JOBS,
    CONFIG,
    PERMISSIONS,
    STORAGE,
    TELEMETRY,
)
from cidx_sdk.capability_types import (
    ApplicationMetrics,
    BackgroundJobsCapability,
    ConfigCapability,
    PermissionsCapability,
    StorageCapability,
    TelemetryCapability,
)
from cidx_sdk.slots import CapabilitySlot

from cidx_xray.config import default_values

#: The feature's context, from ``on_startup`` to ``on_shutdown``.
CONTEXT: CapabilitySlot[FeatureContext] = CapabilitySlot("xray feature context")
#: The dedicated X-Ray thread pool.
EXECUTOR: CapabilitySlot[ThreadPoolExecutor] = CapabilitySlot("xray executor")
#: The X-Ray cell limiter (a resizable limiter: acquire/release/set_limit).
CELL_LIMITER: CapabilitySlot[Any] = CapabilitySlot("xray cell limiter")
#: The execution backend ``xray_backend`` selected (empty under ``local``).
EXECUTION_BACKEND: CapabilitySlot[Any] = CapabilitySlot("xray execution backend")


def context() -> Optional[FeatureContext]:
    """The started feature's context, or None outside a started feature."""
    return CONTEXT.get()


def _capability(name: str) -> Optional[object]:
    ctx = CONTEXT.get()
    if ctx is None or not ctx.has_capability(name):
        return None
    return ctx.capability(name)


def config_section() -> Dict[str, object]:
    """The ``xray`` config section's current values (the ``config``
    capability); its defaults when the feature is not started.

    Raises whatever the config capability raises (a database outage): the
    caller decides how to degrade.
    """
    config = _capability(CONFIG)
    if config is None:
        return default_values()
    values = default_values()
    values.update(cast(ConfigCapability, config).section())
    return values


def application_metrics() -> Optional[ApplicationMetrics]:
    """The active application metrics (``telemetry`` capability), or None."""
    telemetry = _capability(TELEMETRY)
    if telemetry is None:
        return None
    return cast(TelemetryCapability, telemetry).application_metrics()


def background_jobs() -> Optional[BackgroundJobsCapability]:
    """The ``background_jobs`` capability, or None when not started."""
    jobs = _capability(BACKGROUND_JOBS)
    return None if jobs is None else cast(BackgroundJobsCapability, jobs)


def permissions() -> Optional[PermissionsCapability]:
    """The ``permissions`` capability, or None when not started."""
    perms = _capability(PERMISSIONS)
    return None if perms is None else cast(PermissionsCapability, perms)


def storage() -> Optional[StorageCapability]:
    """The ``storage`` capability, or None when not started."""
    store = _capability(STORAGE)
    return None if store is None else cast(StorageCapability, store)


def executor() -> Optional[ThreadPoolExecutor]:
    """The dedicated X-Ray thread pool, or None when not started."""
    return EXECUTOR.get()


def cell_limiter() -> Optional[Any]:
    """The X-Ray cell limiter, or None when not started (CLI, tests)."""
    return CELL_LIMITER.get()
