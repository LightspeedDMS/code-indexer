"""
Pluggable SCIP execution backend: where a project's SCIP indexer runs.

Resolved with the SDK's seam resolver (``cidx_sdk.seams.SeamResolver``, the
mechanism behind the core's ``backend_plugin_loader``), as X-Ray resolves its
own. Under
``scip_backend='delegated-plugin'`` the compile step must not run in the cidx
pod (no toolchain, no repository mount), so the ``ScipExecutionBackend``
(``cidx_scip/delegation_api.py``) comes from a plugin -- evo-sandbox's
``cidx-content-backend`` registers one that runs sandbox-mcp's ``scip`` run
profile and returns the index bytes. It is only resolved for that backend, so
``local`` deployments never import a plugin.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying
``ScipExecutionBackend`` (``build_index`` / ``is_available``). It builds on
``cidx_scip.delegation_api``, the public API cidx keeps stable for it.

Selection (first match wins)
----------------------------
1. ``CIDX_SCIP_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.scip_backend`` group.
3. Nothing -- startup refuses ``delegated-plugin`` without one.

Misconfiguration fails LOUD (``ScipBackendPluginError``): a silent fallback
would build locally in a pod that has neither the toolchain nor the repository.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, List, Optional

from cidx_sdk.entry_points import SCIP_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.errors import ScipBackendPluginError as ScipBackendPluginError
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import DELEGATED_MODE

ENV_VAR = "CIDX_SCIP_BACKEND"
ENTRY_POINT_GROUP = SCIP_BACKEND_ENTRY_POINT_GROUP


def _installed_entry_points(group: str) -> List[Any]:
    """*group*'s installed entry points, through this module's
    ``entry_points`` looked up when called (tests patch it)."""
    return list(entry_points(group=group))


_resolver = SeamResolver(
    seam="scip",
    entry_point_group=ENTRY_POINT_GROUP,
    env_var=ENV_VAR,
    error_type=ScipBackendPluginError,
    display_name="SCIP execution",
    entry_point_source=_installed_entry_points,
)


def resolve_scip_backend() -> Optional[Any]:
    """Instantiate the configured SCIP execution backend, or None if none is."""
    factory = _resolver.find_factory(DELEGATED_MODE)
    if factory is None:
        return None
    return factory()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()


def plugin_factory_available() -> bool:
    """Whether a delegated SCIP backend factory is configured (the env var
    or an entry point); the factory is found, not called. Raises when one is
    configured but cannot be loaded."""
    return _resolver.find_factory(DELEGATED_MODE) is not None
