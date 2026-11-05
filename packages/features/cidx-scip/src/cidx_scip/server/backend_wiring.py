"""SCIP backend wiring helper for CIDX server startup.

Mirrors ``clone_backend_wiring.build_snapshot_manager``: reads the
``scip_backend`` config value and builds a ``SCIPGenerator`` with the matching
indexer backend injected.

Fail-loud policy:
- ``delegated-plugin``: the compile step runs in a ``code_indexer.scip_backend``
  PLUGIN's execution backend (``CIDX_SCIP_BACKEND`` overrides the entry point;
  see ``cidx_scip.server.backend_plugin``), which RETURNS the index bytes. No
  plugin, a plugin that fails to load or construct, one that is not a
  ``ScipExecutionBackend``, or one reporting itself unavailable fails loud --
  never a local build.
- ``local``: no external validation needed (default, unchanged behaviour).
- ``delegated`` (removed: sandbox-mcp's ``sandbox_scip`` writing ``index.scip``
  onto the shared golden volume) and any other value raise
  :class:`UnsupportedScipBackendError`, which stops server startup.

SCIP queries have one path, this pod's ``index.scip.db`` files. The removed
``scip_query_backend`` setting is refused by the core at startup
(``code_indexer.server.startup.removed_scip_settings``), feature or not.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from cidx_scip.server._core import format_error_log

logger = logging.getLogger(__name__)

_NO_LOCAL_FALLBACK = "There is NO fallback to a local build."

#: Logged when startup finds a SCIP config error it does not stop for.
STARTUP_CONFIG_ERROR_CODE = "APP-GENERAL-65021"


class UnsupportedScipBackendError(RuntimeError):
    """``scip_backend`` names a removed or unknown backend.

    Unlike every other SCIP config error this one stops server startup
    (:func:`check_scip_backend_at_startup`): no SCIP build can ever succeed on
    such a pod, and a value that was valid once (``delegated``) is only ever
    there because the deployment was not migrated.
    """


def _resolve_scip_execution_backend() -> Any:
    """The plugin's ``ScipExecutionBackend``, or RuntimeError -- never None."""
    # Imported lazily so local deployments never import the seam.
    from cidx_scip.delegation_api import (
        ScipExecutionBackend,
    )
    from cidx_scip.server.backend_plugin import (
        ENTRY_POINT_GROUP,
        ENV_VAR,
        resolve_scip_backend,
    )

    try:
        backend = resolve_scip_backend()
    except Exception as exc:
        raise RuntimeError(
            "scip_backend='delegated-plugin' selected but the SCIP backend plugin "
            f"could not be loaded or constructed: {type(exc).__name__}: {exc}. "
            f"{_NO_LOCAL_FALLBACK}"
        ) from exc
    if backend is None:
        raise RuntimeError(
            "scip_backend='delegated-plugin' selected but no SCIP backend plugin is "
            "installed. Install a plugin registering a factory on the "
            f"{ENTRY_POINT_GROUP} entry point (cidx-content-backend >= 0.8.0), or "
            f"set {ENV_VAR}='package.module:factory'. {_NO_LOCAL_FALLBACK}"
        )
    if not isinstance(backend, ScipExecutionBackend):
        raise RuntimeError(
            "scip_backend='delegated-plugin' selected but the SCIP backend plugin "
            f"returned a {type(backend).__name__}, which has no "
            f"build_index/is_available. {_NO_LOCAL_FALLBACK}"
        )
    return backend


def validate_scip_backend_config(config: Any) -> None:
    """Eagerly validate the static ``scip_backend`` config.

    The only production caller of :func:`build_scip_generator`
    (``scip_lifecycle.funnel_scip_generation``) wraps the whole build in a
    blanket ``except Exception -> SKIPPED``, so a *configuration* error would
    otherwise be swallowed per-job as an ordinary fail-soft skip, with nothing
    naming the bad value. :func:`check_scip_backend_at_startup` calls this once
    at boot so the misconfiguration surfaces there.

    Raises
    ------
    UnsupportedScipBackendError
        ``scip_backend`` is a removed value (the message says what to set
        instead) or an unrecognised one.
    RuntimeError
        ``delegated-plugin`` is selected but its plugin is missing or broken.
    """
    from cidx_scip.indexers.factory import (
        REMOVED_SCIP_BACKENDS,
        ScipBackendFactory,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"

    removed = REMOVED_SCIP_BACKENDS.get(backend_type)
    if removed is not None:
        raise UnsupportedScipBackendError(removed)

    #  fail-loud: an unrecognised scip_backend value must NOT silently
    # downgrade to a local build. A typo would otherwise come up "healthy" as
    # local and then fail every project with tool-not-found on the
    # toolchain-less indexing pods, with nothing naming the bad value.
    if backend_type not in ScipBackendFactory._VALID_BACKENDS:
        raise UnsupportedScipBackendError(
            f"Unsupported scip_backend={backend_type!r}. Valid options: "
            f"{', '.join(ScipBackendFactory._VALID_BACKENDS)}. "
            "There is NO silent fallback to a local build — fix the "
            "scip_backend config value."
        )

    if backend_type == ScipBackendFactory.DELEGATED_PLUGIN:
        _resolve_scip_execution_backend()


def check_scip_backend_at_startup(get_config: Callable[[], Any]) -> None:
    """Validate ``scip_backend`` once at server startup.

    A removed or unknown value (:class:`UnsupportedScipBackendError`) is
    re-raised and stops startup: the pod crash-loops with the migration message
    instead of serving with SCIP permanently broken. Any other error -- reading
    the config, or a missing or broken ``delegated-plugin`` plugin -- is logged
    at ERROR under :data:`STARTUP_CONFIG_ERROR_CODE` and startup continues, so
    query serving is not taken down; per-job builds then fail-soft to SKIPPED.

    Parameters
    ----------
    get_config:
        Returns the server config; called inside the guarded block so a config
        read failure stays non-fatal.

    Raises
    ------
    UnsupportedScipBackendError
        Propagated from :func:`validate_scip_backend_config`.
    """
    try:
        validate_scip_backend_config(get_config())
    except UnsupportedScipBackendError:
        raise
    except Exception as exc:
        logger.error(
            format_error_log(
                STARTUP_CONFIG_ERROR_CODE,
                f"Invalid scip_backend configuration at startup: {exc}",
            ),
            exc_info=True,
        )
        return
    logger.info("scip_backend config validated at startup")


def build_scip_generator(
    config: Any,
    repo_root: Path,
    *,
    workspace_id: Optional[str] = None,
    golden_alias: Optional[str] = None,
    allow_degenerate: bool = False,
    max_workers: int = 4,
) -> Any:
    """Build a ``SCIPGenerator`` configured from *config*.

    Parameters
    ----------
    config:
        Server config object (``ServerConfig``). Must have a ``scip_backend``
        field (str: ``"local"`` or ``"delegated-plugin"``).
    repo_root:
        Golden-repo root (== the sandbox ``/workspace`` mount).
    workspace_id:
        workspace-mcp id of the repo, threaded into the generator. Mutually
        exclusive with ``golden_alias`` at delegated generate time.
    golden_alias:
        Golden-repo alias ( Option C), threaded into the generator so
        the delegated backend addresses the sandbox build BY ALIAS. Mutually
        exclusive with ``workspace_id``.
    allow_degenerate:
        . Threaded into ``SCIPGenerator`` and forwarded by the delegated
        backend into the build request. ``True`` (set for golden repos) lets a
        bare/unbuilt repo emit a locals-only index instead of exit-71; ``False``
        (default) for the non-golden / local paths.
    max_workers:
        Passed straight through to ``SCIPGenerator``.

    Returns
    -------
    SCIPGenerator
        Configured with the appropriate indexer map injected.

    Raises
    ------
    RuntimeError
        A removed or unknown ``scip_backend`` value, or ``delegated-plugin``
        whose plugin is missing, broken or unavailable. No BACKEND fallback is
        performed.
    """
    from cidx_scip.generator import SCIPGenerator
    from cidx_scip.server.repo_reads import (
        build_file_source,
    )
    from cidx_scip.indexers.factory import ScipBackendFactory

    backend_type = getattr(config, "scip_backend", "local") or "local"
    logger.info("Building SCIPGenerator with scip_backend=%r", backend_type)

    # Shared with the eager startup check so both paths reject the same values
    # BEFORE constructing anything (DRY) — see validate_scip_backend_config.
    validate_scip_backend_config(config)

    # Discovery reads go where the repository is: this pod's disk, or the
    # content seam when the repository is not here (scip_repo_reads).
    discovery_source = build_file_source(repo_root)

    if backend_type == ScipBackendFactory.DELEGATED_PLUGIN:
        backend = _resolve_scip_execution_backend()
        indexer_map = ScipBackendFactory.create_indexer_map(
            backend_type, repo_root=Path(repo_root), execution_backend=backend
        )
        # An unreachable backend at wiring time raises (the funnel turns it
        # into a loud SKIPPED).
        if not next(iter(indexer_map.values())).is_available():
            raise RuntimeError(
                f"SCIP backend plugin {type(backend).__name__} reports itself "
                f"unavailable (sandbox-mcp unreachable?). {_NO_LOCAL_FALLBACK}"
            )
        logger.info(
            "SCIPGenerator: using PluginScipIndexer (%s)", type(backend).__name__
        )
        return SCIPGenerator(
            Path(repo_root),
            max_workers=max_workers,
            indexer_map=indexer_map,
            workspace_id=workspace_id,
            golden_alias=golden_alias,
            allow_degenerate=allow_degenerate,
            build_file_source=discovery_source,
        )

    # "local" (validated above — any other value already raised).
    logger.info("SCIPGenerator: using local subprocess indexers")
    return SCIPGenerator(
        Path(repo_root),
        max_workers=max_workers,
        workspace_id=workspace_id,
        golden_alias=golden_alias,
        allow_degenerate=allow_degenerate,
        build_file_source=discovery_source,
    )


def build_scip_generator_for_golden_repo(
    config: Any,
    repo_root: Path,
    *,
    golden_repo_manager: Any,
    alias: str,
    max_workers: int = 4,
) -> Optional[Any]:
    """Build a ``SCIPGenerator`` for a golden repo, addressed BY ALIAS.

     **seam #3**, updated by  (Option C). Under a delegated
    backend (``delegated-plugin``) the build is addressed by the golden repo's
    own ``alias``, not a workspace-mcp ``workspace_id``: cidx's golden repos are
    cidx-owned dirs at ``/data/data/golden-repos/<alias>`` and carry no
    workspace_id. cidx sends the bare alias verbatim; sandbox-mcp resolves
    ``<GOLDEN_REPOS_ROOT>/<alias>`` and owns the confinement (cidx never builds a
    path). Addressing by alias is the only mode that works for golden repos, so
    it is not gated behind a config knob.

    For the ``local`` backend the alias is irrelevant — it delegates straight to
    :func:`build_scip_generator` with neither addressing param set.

    ``golden_repo_manager`` is retained in the signature for call-site
    compatibility but is not consulted.

    Parameters
    ----------
    config:
        Server config (``ServerConfig``) — read for ``scip_backend``.
    repo_root:
        Golden-repo root (== the sandbox ``/workspace`` mount).
    golden_repo_manager:
        The ``GoldenRepoManager`` (retained for call-site compatibility; not
        consulted for addressing under Option C).
    alias:
        Golden-repo alias — sent verbatim to the sandbox as ``golden_alias``.
    max_workers:
        Passed straight through to ``SCIPGenerator``.

    Returns
    -------
    Optional[SCIPGenerator]
        A configured generator; a delegated golden repo is always addressable.

    Raises
    ------
    RuntimeError
        Propagated from :func:`build_scip_generator`.
    """
    del golden_repo_manager  # no workspace_id resolution — alias-addressed.
    from cidx_scip.indexers.factory import (
        is_delegated_scip_backend,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"

    golden_alias: Optional[str] = (
        alias if is_delegated_scip_backend(backend_type) else None
    )

    # golden-repo SCIP builds are best-effort — a bare (unbuilt) golden
    # repo should get a locals-only (degenerate) index, not skip. Opt every golden
    # repo into the sandbox's allow_degenerate path. (Ignored by the local backend
    # and by non-golden builds, which pass allow_degenerate=False.)
    return build_scip_generator(
        config,
        repo_root,
        golden_alias=golden_alias,
        allow_degenerate=True,
        max_workers=max_workers,
    )
