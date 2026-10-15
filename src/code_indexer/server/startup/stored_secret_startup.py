"""Startup wiring for the stored-secret key and its sweep.

``prepare_stored_secret_key`` runs before any credential manager is built
and aborts startup when the key exists but cannot be used. The sweep runs in
a worker thread after the node advertises its capabilities; a failure is
logged and the sweep simply runs again at the next start.

``registry`` is any object exposing ``connection_pool`` (cluster),
``git_credentials`` and ``ci_tokens`` (the BackendRegistry in production);
``app`` is any object with a ``state`` namespace (the FastAPI app).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from pathlib import Path
from typing import Any

import anyio

logger = logging.getLogger(__name__)


def _prepare_sync(server_dir: Path, storage_mode: str, registry: Any) -> None:
    from code_indexer.server.services.stored_secret_key import (
        configure_cluster_key_source,
        load_or_create_key_file,
    )

    if storage_mode == "postgres":
        from code_indexer.server.services.encryption_key_salt import (
            ensure_encryption_key_salt,
        )

        # Legacy writes (while an older node cannot read v2) use the salt file.
        ensure_encryption_key_salt(server_dir, storage_mode)
        configure_cluster_key_source(registry.connection_pool)
    else:
        load_or_create_key_file(server_dir)


async def prepare_stored_secret_key(
    server_dir: Path, storage_mode: str, registry: Any
) -> None:
    """Create or load the stored-secret key off the event loop.

    Raises StoredSecretKeyError for an unreadable or corrupt key file."""
    await anyio.to_thread.run_sync(
        functools.partial(_prepare_sync, Path(server_dir), storage_mode, registry)
    )


async def _run_sweep(server_dir: Path, registry: Any) -> None:
    from code_indexer.server.services.stored_secret_sweep import (
        run_stored_secret_sweep,
    )

    try:
        await anyio.to_thread.run_sync(
            functools.partial(run_stored_secret_sweep, server_dir, registry)
        )
    except Exception:
        logger.exception("Stored-secret sweep failed; it runs again at next start")


def start_stored_secret_sweep(app: Any, server_dir: Path, registry: Any) -> None:
    """Schedule the stored-secret sweep in a worker thread."""
    app.state.stored_secret_sweep_task = asyncio.create_task(
        _run_sweep(Path(server_dir), registry)
    )


async def stop_stored_secret_sweep(app: Any) -> None:
    """Cancel and await the sweep task at shutdown (a sweep already inside its
    worker thread finishes its current pass first)."""
    task = getattr(app.state, "stored_secret_sweep_task", None)
    if task is None:
        return
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
