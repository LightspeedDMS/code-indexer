"""
X-Ray execution backend wiring helper for CIDX server startup.

The X-Ray sibling of ``ripgrep_backend_wiring``. Selects where X-Ray compiles
and runs caller-supplied evaluators, by the ``xray_backend`` server config
field (see ``xray/execution_backend.py`` for the seam).

Fail-fast policy (mirrors the git, content and ripgrep wiring):
- ``local``     : the evaluator is compiled and run in this pod by xray-cli.
                  This is the DEFAULT, so an unset or absent config behaves
                  exactly as cidx does today. Nothing is built here: each
                  ``RustNativeBackend`` executes in-process with its own caches.
- ``delegated`` : the evaluator runs wherever the X-Ray backend PLUGIN runs
                  it (``code_indexer.xray_backend`` entry point, or
                  ``CIDX_XRAY_BACKEND``; see ``services/xray_backend_plugin``)
                  -- evo-sandbox's runs it in sandbox-mcp. There is
                  deliberately **no fallback to local**: delegation exists so
                  caller-supplied code never runs in the cidx pod, and a pod
                  built for it may hold neither the repositories nor rustc. No
                  plugin installed, a plugin that fails to load or construct,
                  or one that is not an ``XrayExecutionBackend`` stops startup.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.xray.execution_backend import XrayExecutionBackend

logger = logging.getLogger(__name__)

VALID_XRAY_BACKENDS = ("local", "delegated")

_NO_LOCAL_FALLBACK = (
    "Refusing to fall back to local execution: delegation exists so "
    "caller-supplied evaluators never compile or run in the cidx pod."
)


def build_configured_xray_execution_backend(
    config: Any,
) -> Optional[XrayExecutionBackend]:
    """The X-Ray execution backend *config* selects; None means local.

    ``config`` is read via ``getattr`` so a partial/bootstrap config still
    gets the ``local`` default rather than an AttributeError.

    Raises:
        RuntimeError: ``xray_backend`` is unrecognised, or is ``delegated``
            and no usable plugin backend could be built.
    """
    backend_type = getattr(config, "xray_backend", "local") or "local"

    if backend_type not in VALID_XRAY_BACKENDS:
        raise RuntimeError(
            f"Unknown xray_backend {backend_type!r}. "
            f"Valid values are {VALID_XRAY_BACKENDS}."
        )

    if backend_type == "delegated":
        return _build_delegated_backend()

    logger.info("XrayExecutionBackend: using local xray-cli execution (this pod)")
    return None


def _build_delegated_backend() -> XrayExecutionBackend:
    """The plugin's backend, or RuntimeError -- never None, never local."""
    # Imported lazily so the local default never imports the plugin seam.
    from code_indexer.server.services.xray_backend_plugin import (
        ENTRY_POINT_GROUP,
        ENV_VAR,
        resolve_xray_backend,
    )

    try:
        backend = resolve_xray_backend()
    except Exception as exc:
        raise RuntimeError(
            "xray_backend='delegated' selected but the X-Ray backend plugin "
            f"could not be loaded or constructed: {type(exc).__name__}: {exc}. "
            f"{_NO_LOCAL_FALLBACK}"
        ) from exc
    if backend is None:
        raise RuntimeError(
            "xray_backend='delegated' selected but no X-Ray backend plugin is "
            "installed. Install a plugin registering a factory on the "
            f"{ENTRY_POINT_GROUP} entry point (cidx-content-backend >= 0.7.0), "
            f"or set {ENV_VAR}='package.module:factory'. {_NO_LOCAL_FALLBACK}"
        )
    if not isinstance(backend, XrayExecutionBackend):
        raise RuntimeError(
            "xray_backend='delegated' selected but the X-Ray backend plugin "
            f"returned a {type(backend).__name__}, which has no "
            f"execute_scan/execute_graph_analysis. {_NO_LOCAL_FALLBACK}"
        )
    logger.info(
        "XrayExecutionBackend: using delegated execution (%s)",
        type(backend).__name__,
    )
    return backend


def server_xray_execution_backend() -> Optional[XrayExecutionBackend]:
    """The backend the running server built at startup, or None.

    None outside a server process (CLI, tests), before startup has built one,
    and under ``xray_backend=local``; ``RustNativeBackend`` then executes
    in-process, as before. Read without importing the app, so a CLI process
    never constructs it.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    return getattr(_running_server_app_state(), "xray_execution_backend", None)
