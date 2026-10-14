"""
Content backend wiring helper for CIDX server startup.

The content sibling of ``git_backend_wiring``. Builds the ``ContentBackend``
that repository reads run through, selected by the ``content_backend`` server
config field.

Fail-fast policy (mirrors the git and clone wiring):
- ``local``     : reads this container's filesystem. This is the DEFAULT, so an
                  unset or absent config behaves exactly as cidx does today.
- ``delegated`` : bytes come from the service that owns filesystem access, and
                  cidx reads no repository files itself. There is deliberately
                  **no silent fallback to local**.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.

Why the no-fallback rule is stricter here than elsewhere: a pod configured for
delegation may not mount the repositories at all. Falling back to the local
filesystem would not raise -- ``os.walk`` over a missing tree yields nothing --
so the run would report an EMPTY repository and index zero files while looking
entirely successful. An error is the only safe failure.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from code_indexer.server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
)

logger = logging.getLogger(__name__)

VALID_CONTENT_BACKENDS = ("local", "delegated")


def build_content_backend(
    config: Any, *, delegated_backend: Optional[ContentBackend] = None
) -> ContentBackend:
    """Build the ContentBackend configured by *config*.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Read via ``getattr`` so callers
        holding a partial/bootstrap config still get the ``local`` default
        rather than an AttributeError.
    delegated_backend:
        The plugin-supplied backend used when ``content_backend='delegated'``.
        Resolved from the ``code_indexer.content_backend`` entry-point seam.
        Selecting ``delegated`` without one raises.

    Returns
    -------
    ContentBackend
        ``LocalContentBackend`` by default; the injected backend when delegated.

    Raises
    ------
    RuntimeError
        If ``content_backend`` is unrecognised, or ``delegated`` was selected
        with no backend available.
    """
    backend_type = getattr(config, "content_backend", "local") or "local"
    return build_content_backend_for(backend_type, delegated_backend=delegated_backend)


def build_content_backend_for(
    backend_type: str, *, delegated_backend: Optional[ContentBackend] = None
) -> ContentBackend:
    """Build the ContentBackend named by *backend_type*.

    The selection rules of :func:`build_content_backend`, for a caller that
    holds the name rather than a server config -- the ``cidx index`` child,
    which receives it as ``--content-backend`` from the server that spawned it.
    """
    if backend_type not in VALID_CONTENT_BACKENDS:
        raise RuntimeError(
            f"Unknown content_backend {backend_type!r}. "
            f"Valid values are {VALID_CONTENT_BACKENDS}."
        )

    if backend_type == "delegated":
        if delegated_backend is None:
            raise RuntimeError(
                "content_backend='delegated' selected but no delegated backend "
                "is available. Install a plugin registering a factory on the "
                "code_indexer.content_backend entry point, or set "
                "CIDX_CONTENT_BACKEND='package.module:factory'. Refusing to "
                "fall back to the local filesystem: a pod built for delegation "
                "may not mount the repositories at all, and the fallback would "
                "report an empty repository instead of failing."
            )
        backend = ContentBackendFactory.create(
            "delegated", delegated_backend=delegated_backend
        )
        logger.info("ContentBackend: using delegated backend")
        return backend

    backend = ContentBackendFactory.create("local")
    logger.info("ContentBackend: using LocalContentBackend (this container's disk)")
    return backend


def build_configured_content_backend(config: Any) -> ContentBackend:
    """Resolve the delegated plugin (if any) and build the configured backend.

    The single call the startup path uses, so every consumer shares one
    selection. With ``content_backend`` unset/``local`` (the default) the plugin
    is never imported or instantiated and a plain ``LocalContentBackend`` is
    returned -- byte-for-byte the behaviour cidx has today.
    """
    backend_type = getattr(config, "content_backend", "local") or "local"
    return build_named_content_backend(backend_type)


def build_named_content_backend(backend_type: str) -> ContentBackend:
    """Resolve the delegated plugin (if *backend_type* asks for it) and build.

    The by-name twin of :func:`build_configured_content_backend`, used where
    only the selection travels -- the ``cidx index`` child process. The plugin
    reads its endpoint and token from the environment, which the child
    inherits from the server, so the name is all that needs passing.
    """
    # Imported lazily so the local-default path never imports the plugin seam
    # (and so a plugin import error only surfaces when delegation is requested).
    from code_indexer.server.services.content_backend_plugin import (
        resolve_delegated_content_backend,
    )

    delegated = (
        resolve_delegated_content_backend() if backend_type == "delegated" else None
    )
    return build_content_backend_for(backend_type, delegated_backend=delegated)


_unknown_backend_logged = False


def content_backend_name_or_none() -> Optional[str]:
    """The configured content backend, or None when the setting cannot be read.

    For callers that must tell "confirmed delegated" from "unknown": skipping
    local-disk work is safe on either, but DESTROYING local state is only
    justified by a confirmed answer. The failure is logged once per process,
    not per call -- callers sit on hot paths (every regex search).
    """
    global _unknown_backend_logged
    try:
        return configured_content_backend_name()
    except Exception as exc:  # the server config, whatever its state
        if not _unknown_backend_logged:
            _unknown_backend_logged = True
            logger.warning("content_backend setting unreadable (%s)", exc)
        return None


def configured_content_backend_name() -> str:
    """The running server's ``content_backend`` selection, ``local`` when unset.

    For code that must behave differently when repository files are NOT on this
    pod (a delegated backend) but has no config object in hand.
    """
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    return getattr(config, "content_backend", "local") or "local"


def server_content_backend() -> Optional[ContentBackend]:
    """The backend the running server built at startup, or None.

    None outside a server process (CLI, tests) or before startup has built
    one; callers then read the local disk, as before. Read without importing
    the app, so a CLI process never constructs it.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    return getattr(_running_server_app_state(), "content_backend", None)


def is_content_seam_delegated() -> bool:
    """True when the running server reads repository files through a NON-local
    backend -- the repositories are not on this pod's disk."""
    backend = server_content_backend()
    return backend is not None and not backend.is_local


def golden_repo_content_root(repo_path: Union[str, Path], *, consumer: str) -> Path:
    """Where a delegated backend can see the repository behind *repo_path*.

    A delegated backend (regex in workspace-mcp, the content seam) sees the
    golden repos' base clones. Refresh, though, points an alias at a versioned
    snapshot it creates on THIS pod's disk -- holding only index state -- which
    the backend cannot see. So a snapshot path maps to its golden repo's
    registered base clone. That is the repository's current checkout, which can
    be newer than the snapshot's index. Every other path is returned unchanged.

    When the registry has no clone for the repo the snapshot path is kept and a
    warning names *consumer*: the read then behaves as it did before the mapping.

    Callers decide whether their backend is delegated; this only maps the path.
    Blocking (a registry read for a snapshot path): call it off the event loop.
    """
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    if not is_versioned_snapshot(str(repo_path)):
        return Path(repo_path)
    repo_name = PurePosixPath(repo_path).parts[-2]
    clone_path = _golden_repo_clone_path(repo_name)
    if clone_path is None:
        logger.warning(
            "%s: no base clone registered for %s; using the snapshot path, "
            "which the delegated backend cannot see",
            consumer,
            repo_name,
        )
        return Path(repo_path)
    return Path(clone_path)


def is_pod_local_golden_path(repo_path: Union[str, Path]) -> bool:
    """Whether the golden repo behind *repo_path* keeps its files on THIS pod.

    *repo_path* is a golden repo's base clone or a ``.versioned/<alias>/v_*``
    snapshot of it. Pod-local means a ``local://`` repository other than
    cidx-meta (Langfuse trace folders): created and written by this pod, never
    held by a delegated backend, so a delegated read must not be sent there.
    cidx-meta is ``local://`` too, but its content is shared through the
    backend. A repo the registry cannot describe is not pod-local -- the
    delegated seam, as before.

    Blocking (a registry read): call it off the event loop.
    """
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )
    from code_indexer.server.utils.index_command_layout import (
        is_cidx_meta_repo,
        is_pod_local_repo,
    )

    parts = PurePosixPath(str(repo_path)).parts
    if not parts:
        return False
    snapshot = is_versioned_snapshot(str(repo_path))
    alias = parts[-2] if snapshot else parts[-1]
    repo = _golden_repo_record(alias)
    repo_url = getattr(repo, "repo_url", None) or None
    if not snapshot:
        # Only the golden repo's own directory: an activated copy (or any
        # other path) that merely ends in the same name is not it.
        clone_path = getattr(repo, "clone_path", None)
        if not clone_path or os.path.normpath(str(clone_path)) != os.path.normpath(
            str(repo_path)
        ):
            return False
    return is_pod_local_repo(repo_url, alias) and not is_cidx_meta_repo(repo_url, alias)


def _golden_repo_record(repo_name: str) -> Any:
    """The golden repo's registry record, or None when the registry cannot say."""
    try:
        from code_indexer.server.repositories.golden_repo_manager import (
            get_golden_repo_manager,
        )

        return get_golden_repo_manager().get_golden_repo(repo_name)
    except Exception as exc:  # the server's state, whatever it is
        logger.debug("No golden repo record for %s: %s", repo_name, exc)
        return None


def _golden_repo_clone_path(repo_name: str) -> Optional[str]:
    """The golden repo's base clone path, or None when the registry cannot say."""
    repo = _golden_repo_record(repo_name)
    return getattr(repo, "clone_path", None) or None
