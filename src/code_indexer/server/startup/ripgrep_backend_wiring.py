"""
Ripgrep backend wiring helper for CIDX server startup.

The ripgrep sibling of ``content_backend_wiring``. Builds the ``RipgrepBackend``
that regex search runs through, selected by the ``ripgrep_backend`` server
config field.

Fail-fast policy (mirrors the git, clone and content wiring):
- ``local``     : runs the binary in this container. The DEFAULT, so an unset or
                  absent config behaves exactly as cidx does today.
- ``delegated`` : the argv is executed by the service that owns the
                  repositories. There is deliberately **no silent fallback**.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.

An image built for delegation may not ship the ripgrep binary at all, so a
fallback would not quietly do the wrong thing -- it would fail at exec time,
somewhere far from the configuration that caused it.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Tuple

from code_indexer.server.storage.shared.ripgrep_backend import (
    RipgrepBackend,
    RipgrepBackendFactory,
)

logger = logging.getLogger(__name__)

VALID_RIPGREP_BACKENDS = ("local", "delegated")


def build_ripgrep_backend(
    config: Any,
    *,
    delegated_backend: Optional[RipgrepBackend] = None,
    max_workers: int = 2,
) -> RipgrepBackend:
    """Build the RipgrepBackend configured by *config*.

    ``config`` is read via ``getattr`` so a partial/bootstrap config still gets
    the ``local`` default rather than an AttributeError.

    Raises:
        RuntimeError: if ``ripgrep_backend`` is unrecognised, or ``delegated``
            was selected with no backend available.
    """
    backend_type = getattr(config, "ripgrep_backend", "local") or "local"

    if backend_type not in VALID_RIPGREP_BACKENDS:
        raise RuntimeError(
            f"Unknown ripgrep_backend {backend_type!r}. "
            f"Valid values are {VALID_RIPGREP_BACKENDS}."
        )

    if backend_type == "delegated":
        if delegated_backend is None:
            raise RuntimeError(
                "ripgrep_backend='delegated' selected but no delegated backend "
                "is available. Install a plugin registering a factory on the "
                "code_indexer.ripgrep_backend entry point, or set "
                "CIDX_RIPGREP_BACKEND='package.module:factory'. Refusing to "
                "fall back to local ripgrep: an image built for delegation may "
                "not ship the binary at all."
            )
        backend = RipgrepBackendFactory.create(
            "delegated", delegated_backend=delegated_backend
        )
        logger.info("RipgrepBackend: using delegated backend")
        return backend

    backend = RipgrepBackendFactory.create("local", max_workers=max_workers)
    logger.info("RipgrepBackend: using LocalRipgrepBackend (this container)")
    return backend


def build_configured_ripgrep_backend(
    config: Any, *, max_workers: int = 2
) -> RipgrepBackend:
    """Resolve the delegated plugin (if any) and build the configured backend.

    With ``ripgrep_backend`` unset/``local`` (the default) the plugin is never
    imported or instantiated and a plain ``LocalRipgrepBackend`` is returned --
    byte-for-byte the behaviour cidx has today.
    """
    # Imported lazily so the local-default path never imports the plugin seam.
    from code_indexer.server.services.ripgrep_backend_plugin import (
        resolve_delegated_ripgrep_backend,
    )

    backend_type = getattr(config, "ripgrep_backend", "local") or "local"
    delegated = (
        resolve_delegated_ripgrep_backend() if backend_type == "delegated" else None
    )
    return build_ripgrep_backend(
        config, delegated_backend=delegated, max_workers=max_workers
    )


def server_ripgrep_backend() -> Optional[RipgrepBackend]:
    """The backend the running server built at startup, or None.

    None outside a server process (CLI, tests) or before startup has built
    one; the regex service then uses ``LocalRipgrepBackend``, as before. Read
    without importing the app, so a CLI process never constructs it.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    return getattr(_running_server_app_state(), "ripgrep_backend", None)


def server_regex_target(
    repo_path: Path,
) -> Tuple[Path, Optional[RipgrepBackend]]:
    """Where a server regex search runs, and the backend that runs it.

    A delegated backend runs ripgrep in workspace-mcp, which holds the golden
    repos' base clones. Refresh, though, points an alias at a versioned
    snapshot it creates on THIS pod's disk, which workspace-mcp cannot see --
    so such a path is searched at its golden repo's base clone instead. That
    is the repository's current checkout, which can be newer than the
    snapshot's index. Every other path, and every local search, is unchanged.

    Blocking (a registry read for a snapshot path): call it off the event loop.
    """
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    backend = server_ripgrep_backend()
    if backend is None or backend.is_local or not is_versioned_snapshot(str(repo_path)):
        return Path(repo_path), backend
    repo_name = PurePosixPath(repo_path).parts[-2]
    clone_path = _golden_repo_clone_path(repo_name)
    if clone_path is None:
        logger.warning(
            "Regex search: no base clone registered for %s; searching the "
            "snapshot path, which the delegated backend cannot see",
            repo_name,
        )
        return Path(repo_path), backend
    return Path(clone_path), backend


def _golden_repo_clone_path(repo_name: str) -> Optional[str]:
    """The golden repo's base clone path, or None when the registry cannot say."""
    try:
        from code_indexer.server.repositories.golden_repo_manager import (
            get_golden_repo_manager,
        )

        repo = get_golden_repo_manager().get_golden_repo(repo_name)
    except Exception as exc:  # the server's state, whatever it is
        logger.debug("No golden repo record for %s: %s", repo_name, exc)
        return None
    return getattr(repo, "clone_path", None) or None
