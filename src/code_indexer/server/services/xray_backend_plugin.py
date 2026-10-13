"""
Pluggable X-Ray execution backend: where caller-supplied evaluators run.

The sixth seam to use ``backend_plugin_loader``, after git, content, ripgrep,
index artifacts and the content writer. Under ``xray_backend='delegated'`` an
evaluator must not compile or run in the cidx pod, so the
``XrayExecutionBackend`` (``xray/execution_backend.py``) comes from a plugin --
evo-sandbox's ``cidx-content-backend`` registers one that runs xray-cli in
sandbox-mcp. It is only ever resolved when ``xray_backend`` is delegated, so a
local deployment never imports a plugin.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying
``XrayExecutionBackend`` (``execute_scan`` / ``execute_graph_analysis``). It
shapes results with ``code_indexer.xray.delegation_api``, the public API cidx
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

from typing import Any, Optional

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_XRAY_BACKEND"
ENTRY_POINT_GROUP = "code_indexer.xray_backend"


class XrayBackendPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=XrayBackendPluginError,
    seam_name="X-Ray execution",
)


def resolve_xray_backend() -> Optional[Any]:
    """Instantiate the configured X-Ray execution backend, or None if none is."""
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
