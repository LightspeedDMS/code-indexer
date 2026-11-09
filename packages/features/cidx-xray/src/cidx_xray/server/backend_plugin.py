"""
Pluggable X-Ray execution backend: where caller-supplied evaluators run.

Resolved with the SDK's seam resolver (``cidx_sdk.seams.SeamResolver``, the
mechanism behind the core's ``backend_plugin_loader``), like git, content,
ripgrep, index artifacts and the content writer. Under ``xray_backend='delegated'`` an evaluator must not
compile or run in the cidx pod, so the ``XrayExecutionBackend``
(``cidx_xray/execution_backend.py``) comes from a plugin --
evo-sandbox's ``cidx-content-backend`` registers one that runs xray-cli in
the sandbox service. It is only ever resolved when ``xray_backend`` is delegated, so a
local deployment never imports a plugin.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying
``XrayExecutionBackend`` (``execute_scan`` / ``execute_graph_analysis``). It
shapes results with ``cidx_xray.delegation_api``, the public API cidx
keeps stable for it.

Selection (first match wins)
----------------------------
1. ``CIDX_XRAY_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.xray_backend`` group.
3. Nothing -- startup refuses a delegated X-Ray backend without one.

Misconfiguration fails LOUD (``XrayBackendPluginError``): a silent fallback
would compile and run caller-supplied code in this pod, exactly what
delegation exists to prevent.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, List, Optional

from cidx_sdk.entry_points import XRAY_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.errors import XrayBackendPluginError as XrayBackendPluginError
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import DELEGATED_MODE

ENV_VAR = "CIDX_XRAY_BACKEND"
ENTRY_POINT_GROUP = XRAY_BACKEND_ENTRY_POINT_GROUP


def _installed_entry_points(group: str) -> List[Any]:
    """*group*'s installed entry points, through this module's
    ``entry_points`` looked up when called (tests patch it)."""
    return list(entry_points(group=group))


_resolver = SeamResolver(
    seam="xray",
    entry_point_group=ENTRY_POINT_GROUP,
    env_var=ENV_VAR,
    error_type=XrayBackendPluginError,
    display_name="X-Ray execution",
    entry_point_source=_installed_entry_points,
)


def resolve_xray_backend() -> Optional[Any]:
    """Instantiate the configured X-Ray execution backend, or None if none is."""
    factory = _resolver.find_factory(DELEGATED_MODE)
    if factory is None:
        return None
    return factory()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
