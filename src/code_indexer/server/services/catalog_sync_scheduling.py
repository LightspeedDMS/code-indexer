"""issue-64616: the periodic golden-catalog and workspace-registration passes.

Each runs on its own interval, off the event loop, only when its mode
(``CIDX_GOLDEN_CATALOG_SYNC_MODE`` / ``CIDX_WORKSPACE_SYNC_MODE``) is
``dry-run`` or ``apply`` -- both default to ``off``, so a deploy changes
nothing until one is enabled. The mode is read again before every pass, so
switching it off takes effect at the next interval.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


def _catalog_pass(manager: Any) -> Dict[str, Any]:
    from code_indexer.server.services import golden_catalog_sync as catalog

    mode = catalog.sync_mode(catalog.MODE_ENV)
    if mode == "off":
        return {"mode": "off"}
    return catalog.run_sync(catalog.deps_from_manager(manager), mode).as_dict()


def _workspace_pass(manager: Any) -> Dict[str, Any]:
    from code_indexer.server.services import golden_catalog_sync as catalog
    from code_indexer.server.services import workspace_registration_sync as ws

    mode = catalog.sync_mode(ws.MODE_ENV)
    if mode == "off":
        return {"mode": "off"}
    return ws.run_workspace_sync(ws.deps_from_manager(manager), mode)


async def _loop(name: str, interval: int, run: Callable[[], Dict[str, Any]]) -> None:
    import anyio.to_thread as to_thread

    while True:
        try:
            result = await to_thread.run_sync(run)
            if result.get("mode") != "off":
                logger.info("issue-64616: %s pass: %s", name, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a pass failure never stops the loop
            logger.error("issue-64616: %s pass failed: %s", name, exc, exc_info=True)
        await asyncio.sleep(interval)


def start(manager: Any) -> List["asyncio.Task[None]"]:
    """Start both loops (each idles while its mode is off); their tasks."""
    from code_indexer.server.services import golden_catalog_sync as catalog
    from code_indexer.server.services import workspace_registration_sync as ws

    return [
        asyncio.create_task(
            _loop(
                "golden catalog sync",
                catalog.positive_int_env(
                    catalog.INTERVAL_ENV, catalog.DEFAULT_INTERVAL_SECONDS
                ),
                lambda: _catalog_pass(manager),
            )
        ),
        asyncio.create_task(
            _loop(
                "workspace registration sync",
                catalog.positive_int_env(ws.INTERVAL_ENV, ws.DEFAULT_INTERVAL_SECONDS),
                lambda: _workspace_pass(manager),
            )
        ),
    ]


def run_once(manager: Any, *, scope: str, dry_run: bool) -> Dict[str, Any]:
    """One pass of *scope* (``catalog`` | ``workspaces`` | ``all``) now, in
    dry-run or apply, whatever the configured mode (the admin door)."""
    from code_indexer.server.services import golden_catalog_sync as catalog
    from code_indexer.server.services import workspace_registration_sync as ws

    mode = "dry-run" if dry_run else "apply"
    result: Dict[str, Any] = {}
    if scope in ("catalog", "all"):
        result["catalog"] = catalog.run_sync(
            catalog.deps_from_manager(manager), mode
        ).as_dict()
    if scope in ("workspaces", "all"):
        result["workspaces"] = ws.run_workspace_sync(
            ws.deps_from_manager(manager), mode
        )
    return result
