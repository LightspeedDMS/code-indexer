"""
Pluggable SCIP execution backend: where a project's SCIP indexer runs.

The seventh seam to use ``backend_plugin_loader``, after git, content, ripgrep,
index artifacts, the content writer and X-Ray. Under
``scip_backend='delegated-plugin'`` the compile step must not run in the cidx
pod (no toolchain, no repository mount), so the ``ScipExecutionBackend``
(``scip/delegation_api.py``) comes from a plugin -- evo-sandbox's
``cidx-content-backend`` registers one that runs sandbox-mcp's ``scip`` run
profile and returns the index bytes. It is only resolved for that backend, so
``local`` and ``delegated`` (``sandbox_scip``) deployments never import a plugin.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying
``ScipExecutionBackend`` (``build_index`` / ``is_available``). It builds on
``code_indexer.scip.delegation_api``, the public API cidx keeps stable for it.

Selection (first match wins)
----------------------------
1. ``CIDX_SCIP_BACKEND`` env var, as ``"package.module:factory"``.
2. An entry point in the ``code_indexer.scip_backend`` group.
3. Nothing -- startup refuses ``delegated-plugin`` without one.

Misconfiguration fails LOUD (``ScipBackendPluginError``): a silent fallback
would build locally in a pod that has neither the toolchain nor the repository.
"""

from __future__ import annotations

from typing import Any, Optional

from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver

ENV_VAR = "CIDX_SCIP_BACKEND"
ENTRY_POINT_GROUP = "code_indexer.scip_backend"


class ScipBackendPluginError(RuntimeError):
    """A plugin was configured but could not be loaded or is not callable."""


_resolver = BackendPluginResolver(
    env_var=ENV_VAR,
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=ScipBackendPluginError,
    seam_name="SCIP execution",
)


def resolve_scip_backend() -> Optional[Any]:
    """Instantiate the configured SCIP execution backend, or None if none is."""
    return _resolver.resolve()


def reset_cache() -> None:
    """Clear the resolved plugin. For tests."""
    _resolver.reset_cache()
