"""SCIP backend wiring helper for CIDX server startup (EVO-65021).

Mirrors ``clone_backend_wiring.build_snapshot_manager``: reads the
``scip_backend`` config value and builds a ``SCIPGenerator`` with the matching
indexer backend injected, health-checking the delegated backend at construction.

Fail-loud policy (mirrors the ``delegated`` clone backend, EVO-64616):
- ``delegated``: sandbox-mcp reachability MUST pass at wiring time. If it does
  not, raise ``RuntimeError`` immediately — there is NO silent fallback to a
  local build (the toolchain-less indexing pods would fail anyway). A *runtime*
  backend outage is handled separately: ``DelegatedIndexer.generate`` fail-softs
  per project to ``IndexerStatus.FAILED`` so a golden-repo job is never blocked.
- ``delegated-plugin``: the compile step runs in a ``code_indexer.scip_backend``
  PLUGIN's execution backend (``CIDX_SCIP_BACKEND`` overrides the entry point;
  see ``services/scip_backend_plugin``), which RETURNS the index bytes -- the
  mount-less successor of ``delegated``. No plugin, a plugin that fails to load
  or construct, one that is not a ``ScipExecutionBackend``, or one reporting
  itself unavailable fails loud -- never a local build, never ``delegated``.
- ``local``: no external validation needed (default, unchanged behaviour).

``delegated`` keeps meaning ``sandbox_scip`` until cutover, so installing the
plugin (a cidx image bump) changes nothing for a deployment until it opts in by
setting ``delegated-plugin``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NO_LOCAL_FALLBACK = (
    "There is NO fallback to a local build or to the sandbox_scip backend."
)


def _resolve_scip_execution_backend() -> Any:
    """The plugin's ``ScipExecutionBackend``, or RuntimeError -- never None."""
    # Imported lazily so local/delegated deployments never import the seam.
    from code_indexer.scip.delegation_api import (  # noqa: PLC0415
        ScipExecutionBackend,
    )
    from code_indexer.server.services.scip_backend_plugin import (  # noqa: PLC0415
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


def _resolve_scip_sandbox_config(config: Any) -> Any:
    """Return the config block the delegated SCIP backend addresses (EVO-65365).

    The delegated SCIP backend targets the STANDING sandbox-mcp service, which is
    a DIFFERENT service than the workspace-mcp that ``workspace_mcp`` addresses
    for the clone/git delegation backends — one endpoint cannot be both. Prefer
    the dedicated ``scip_sandbox`` block when it is set with a non-empty
    ``endpoint``; otherwise fall back to ``workspace_mcp`` for backward
    compatibility (older configs that overloaded ``workspace_mcp`` for SCIP too).

    This resolves only the ENDPOINT SOURCE — the fail-loud BACKEND selection
    (``scip_backend`` value) is unaffected and never falls back.
    """
    scip_cfg = getattr(config, "scip_sandbox", None)
    if scip_cfg is None or not getattr(scip_cfg, "endpoint", ""):
        return getattr(config, "workspace_mcp", None)
    return scip_cfg


def _check_scip_backend_health(endpoint: str, api_key: str) -> None:
    """Verify sandbox-mcp is reachable before wiring the delegated SCIP backend.

    EVO-65021: the ``delegated`` scip backend forwards every project's compile
    step to sandbox-mcp. If sandbox-mcp is unreachable at startup we fail fast —
    there is NO silent fallback to a local build (which would fail on the
    toolchain-less indexing pods anyway, EVO-65020). GETs ``{endpoint}/health``.
    """
    import requests  # noqa: PLC0415

    health_url = f"{endpoint.rstrip('/')}/health"
    logger.info("Checking sandbox-mcp (scip backend) health at %s", health_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(health_url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"sandbox-mcp not reachable at {endpoint} for the delegated scip "
            f"backend. Ensure sandbox-mcp is running and the config "
            f"scip_sandbox.endpoint (or workspace_mcp.endpoint as a fallback) is "
            f"correct. Error: {exc}"
        ) from exc
    logger.info("sandbox-mcp (scip backend) health check: OK")


def validate_scip_backend_config(config: Any) -> None:
    """Eagerly validate the static ``scip_backend`` config (EVO-65021).

    This is the fail-loud entry point the module's fail-loud policy needs at
    *server startup*. The only production caller of :func:`build_scip_generator`
    (``scip_lifecycle.funnel_scip_generation``) wraps the whole build in a
    blanket ``except Exception -> SKIPPED``, so a *configuration* error — a
    typo'd ``scip_backend`` value, or a ``delegated`` backend with no
    ``scip_sandbox.endpoint`` (nor the ``workspace_mcp.endpoint`` fallback) —
    would otherwise be swallowed per-job as an
    ordinary fail-soft skip, indistinguishable from a legitimate one and with
    nothing naming the bad value. Calling this once at startup (mirroring how
    ``clone_backend_wiring.build_snapshot_manager`` is invoked from
    ``lifespan.py``) surfaces the misconfiguration loudly at boot.

    This validates the *config only* — it does NOT health-check sandbox-mcp
    reachability. A runtime sandbox-mcp outage remains a per-repo fail-soft
    (``DelegatedIndexer.generate`` -> ``IndexerStatus.FAILED`` -> ``SKIPPED``);
    only a static misconfiguration fails loud here.

    Raises
    ------
    RuntimeError
        If ``scip_backend`` is an unrecognised value, or is ``"delegated"``
        with no ``scip_sandbox.endpoint`` (nor the ``workspace_mcp.endpoint``
        fallback) configured.
    """
    from code_indexer.scip.indexers.factory import (  # noqa: PLC0415
        ScipBackendFactory,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"

    # EVO-65021 fail-loud: an unrecognised scip_backend value must NOT silently
    # downgrade to a local build. A typo (e.g. "delegatd") would otherwise come
    # up "healthy" as local and then fail every project with tool-not-found on
    # the toolchain-less indexing pods, with nothing naming the bad value.
    if backend_type not in ScipBackendFactory._VALID_BACKENDS:
        raise RuntimeError(
            f"Unsupported scip_backend={backend_type!r}. Valid options: "
            f"{', '.join(ScipBackendFactory._VALID_BACKENDS)}. "
            "There is NO silent fallback to a local build — fix the "
            "scip_backend config value."
        )

    if backend_type == ScipBackendFactory.DELEGATED_PLUGIN:
        _resolve_scip_execution_backend()
        return

    if backend_type == "delegated":
        wsm_cfg = _resolve_scip_sandbox_config(config)
        if wsm_cfg is None or not getattr(wsm_cfg, "endpoint", ""):
            raise RuntimeError(
                "scip_backend='delegated' requires scip_sandbox.endpoint (or "
                "workspace_mcp.endpoint as a fallback) to be configured (pointed "
                "at the sandbox-mcp service). None was provided."
            )


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
        field (str: ``"local"`` or ``"delegated"``) and, for ``"delegated"``, a
        populated ``scip_sandbox`` block (endpoint + api_key, pointed at the
        standing sandbox-mcp — contract §5), or ``workspace_mcp`` as a fallback
        (EVO-65365) — see :func:`_resolve_scip_sandbox_config`.
    repo_root:
        Golden-repo root (== the sandbox ``/workspace`` mount).
    workspace_id:
        workspace-mcp id of the golden repo (EVO-65021 / stream-02), threaded
        into the generator so the delegated backend can address the sandbox.
        Mutually exclusive with ``golden_alias`` at delegated generate time.
    golden_alias:
        Golden-repo alias (EVO-65365 Option C), threaded into the generator so
        the delegated backend can address the sandbox build BY ALIAS when the
        golden repo has no workspace_id — the standing path for cidx-owned
        golden repos. Mutually exclusive with ``workspace_id``.
    allow_degenerate:
        EVO-65365. Threaded into ``SCIPGenerator`` and forwarded by the delegated
        backend into the sandbox request. ``True`` (set for golden repos) lets a
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
        If ``scip_backend == "delegated"`` and either the resolved
        ``scip_sandbox.endpoint`` (or ``workspace_mcp.endpoint`` fallback) is
        unset or sandbox-mcp is unreachable. No BACKEND fallback is performed.
    """
    from code_indexer.scip.generator import SCIPGenerator  # noqa: PLC0415
    from code_indexer.server.services.scip_repo_reads import (  # noqa: PLC0415
        build_file_source,
    )
    from code_indexer.scip.indexers.factory import (  # noqa: PLC0415
        ScipBackendFactory,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"
    logger.info("Building SCIPGenerator with scip_backend=%r", backend_type)

    # EVO-65021 fail-loud: reject an unrecognised scip_backend value or a
    # delegated backend with no scip_sandbox.endpoint (nor the
    # workspace_mcp.endpoint fallback) BEFORE constructing anything. Shared with
    # the eager startup call site (lifespan.py) so both
    # paths use the identical check (DRY) — see validate_scip_backend_config.
    validate_scip_backend_config(config)

    # Discovery reads go where the repository is: this pod's disk, or the
    # content seam when the repository is not here (scip_repo_reads).
    discovery_source = build_file_source(repo_root)

    if backend_type == ScipBackendFactory.DELEGATED_PLUGIN:
        backend = _resolve_scip_execution_backend()
        indexer_map = ScipBackendFactory.create_indexer_map(
            backend_type, repo_root=Path(repo_root), execution_backend=backend
        )
        # Same fail-fast as ``delegated``'s health check: an unreachable backend
        # at wiring time raises (the funnel turns it into a loud SKIPPED).
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

    if backend_type == "delegated":
        # EVO-65021: delegate the compile step to sandbox-mcp. Fail-fast on an
        # unreachable sandbox-mcp — NO silent local fallback (mirrors the
        # delegated clone backend, EVO-64616). endpoint presence is guaranteed
        # by validate_scip_backend_config above.
        # EVO-65365: address the STANDING sandbox-mcp via ``scip_sandbox`` when
        # set, falling back to ``workspace_mcp`` — see _resolve_scip_sandbox_config.
        wsm_cfg = _resolve_scip_sandbox_config(config)
        _check_scip_backend_health(wsm_cfg.endpoint, wsm_cfg.api_key)
        indexer_map = ScipBackendFactory.create_indexer_map(
            "delegated",
            repo_root=Path(repo_root),
            endpoint_config=wsm_cfg,
        )
        logger.info(
            "SCIPGenerator: using DelegatedIndexer (sandbox-mcp=%s)",
            wsm_cfg.endpoint,
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

    # "local" (validated above — an unrecognised value already raised).
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

    This is the EVO-65021 **seam #3**, updated by EVO-65365 (Option C, cidx
    side). Under ``scip_backend='delegated'`` it now addresses the sandbox build
    by the golden repo's own ``alias`` — NOT a workspace-mcp ``workspace_id``.
    cidx's golden repos are cidx-owned dirs at ``/data/data/golden-repos/<alias>``
    and 0 of them carry a workspace_id, so the old workspace_id path fail-soft
    SKIPPED *every* golden repo ("skipped: No SCIP index available"). Option C
    stands up a sandbox-mcp that mounts cidx's golden FlexVol and accepts a
    ``golden_alias`` addressing the golden repo directly, so a golden repo is now
    ALWAYS addressable — the fail-soft skip is gone. cidx sends the bare alias
    verbatim; the sandbox resolves ``<GOLDEN_REPOS_ROOT>/<alias>`` and owns the
    confinement (cidx never builds a path).

    Addressing by alias is the DEFAULT for delegated golden repos — it is the
    only mode that works for them (there is no workspace_id to resolve), so it is
    not gated behind a config knob.

    For the ``local`` backend the alias is irrelevant — it delegates straight to
    :func:`build_scip_generator` with neither addressing param set, preserving
    today's behaviour.

    ``golden_repo_manager`` is retained in the signature for call-site
    compatibility (and future non-alias needs) but is no longer consulted for a
    workspace_id under Option C.

    Parameters
    ----------
    config:
        Server config (``ServerConfig``) — read for ``scip_backend`` and, when
        delegated, the ``scip_sandbox`` block (endpoint + api_key), falling back
        to ``workspace_mcp`` — see :func:`_resolve_scip_sandbox_config`.
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
        A configured generator. Under Option C a delegated golden repo is always
        addressable, so this no longer returns ``None`` to skip SCIP.

    Raises
    ------
    RuntimeError
        Propagated from :func:`build_scip_generator` when the delegated backend
        is selected but the resolved ``scip_sandbox.endpoint`` (or the
        ``workspace_mcp.endpoint`` fallback) is unset or sandbox-mcp is
        unreachable (a misconfiguration/outage that fails loud at wiring time).
    """
    del golden_repo_manager  # EVO-65365: no workspace_id resolution — alias-addressed.
    from code_indexer.scip.indexers.factory import (  # noqa: PLC0415
        is_delegated_scip_backend,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"

    golden_alias: Optional[str] = (
        alias if is_delegated_scip_backend(backend_type) else None
    )

    # EVO-65365: golden-repo SCIP builds are best-effort — a bare (unbuilt) golden
    # repo should get a locals-only (degenerate) index, not skip. Opt every golden
    # repo into the sandbox's allow_degenerate path. (Ignored by the local backend
    # and the non-golden delegated path, which pass allow_degenerate=False.)
    return build_scip_generator(
        config,
        repo_root,
        golden_alias=golden_alias,
        allow_degenerate=True,
        max_workers=max_workers,
    )
