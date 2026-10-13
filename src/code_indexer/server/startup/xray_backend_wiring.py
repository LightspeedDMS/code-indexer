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
- ``delegated`` : the evaluator runs in sandbox-mcp (``sandbox_xray``). Not
                  implemented yet, so selecting it raises at startup. There is
                  deliberately **no fallback to local**: delegation exists so
                  caller-supplied code never runs in the cidx pod, and a pod
                  built for it may hold neither the repositories nor rustc.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.xray.execution_backend import XrayExecutionBackend

logger = logging.getLogger(__name__)

VALID_XRAY_BACKENDS = ("local", "delegated")


def build_configured_xray_execution_backend(
    config: Any,
) -> Optional[XrayExecutionBackend]:
    """The X-Ray execution backend *config* selects; None means local.

    ``config`` is read via ``getattr`` so a partial/bootstrap config still
    gets the ``local`` default rather than an AttributeError.

    Raises:
        RuntimeError: ``xray_backend`` is unrecognised, or is ``delegated``
            (not implemented yet).
    """
    backend_type = getattr(config, "xray_backend", "local") or "local"

    if backend_type not in VALID_XRAY_BACKENDS:
        raise RuntimeError(
            f"Unknown xray_backend {backend_type!r}. "
            f"Valid values are {VALID_XRAY_BACKENDS}."
        )

    if backend_type == "delegated":
        raise RuntimeError(
            "xray_backend='delegated' selected but delegated X-Ray execution is "
            "not implemented yet: it needs sandbox-mcp's sandbox_xray tool. Set "
            "xray_backend='local'. Refusing to fall back to local execution: "
            "delegation exists so caller-supplied evaluators never compile or "
            "run in the cidx pod."
        )

    logger.info("XrayExecutionBackend: using local xray-cli execution (this pod)")
    return None


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
