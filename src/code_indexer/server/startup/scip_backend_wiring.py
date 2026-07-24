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
- ``local``: no external validation needed (default, unchanged behaviour).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
            f"backend. Ensure sandbox-mcp is running and CIDX_SCIP_BACKEND / "
            f"workspace_mcp.endpoint is correct. Error: {exc}"
        ) from exc
    logger.info("sandbox-mcp (scip backend) health check: OK")


def build_scip_generator(
    config: Any,
    repo_root: Path,
    *,
    workspace_id: Optional[str] = None,
    max_workers: int = 4,
) -> Any:
    """Build a ``SCIPGenerator`` configured from *config*.

    Parameters
    ----------
    config:
        Server config object (``ServerConfig``). Must have a ``scip_backend``
        field (str: ``"local"`` or ``"delegated"``) and, for ``"delegated"``, a
        populated ``workspace_mcp`` block (reused for endpoint + api_key,
        pointed at sandbox-mcp — contract §5).
    repo_root:
        Golden-repo root (== the sandbox ``/workspace`` mount).
    workspace_id:
        workspace-mcp id of the golden repo (EVO-65021 / stream-02), threaded
        into the generator so the delegated backend can address the sandbox.
        Required in practice for ``"delegated"`` at generate time; accepted here
        as a param until stream-02 supplies it.
    max_workers:
        Passed straight through to ``SCIPGenerator``.

    Returns
    -------
    SCIPGenerator
        Configured with the appropriate indexer map injected.

    Raises
    ------
    RuntimeError
        If ``scip_backend == "delegated"`` and either ``workspace_mcp.endpoint``
        is unset or sandbox-mcp is unreachable. No fallback is performed.
    """
    from code_indexer.scip.generator import SCIPGenerator  # noqa: PLC0415
    from code_indexer.scip.indexers.factory import (  # noqa: PLC0415
        ScipBackendFactory,
    )

    backend_type = getattr(config, "scip_backend", "local") or "local"
    logger.info("Building SCIPGenerator with scip_backend=%r", backend_type)

    if backend_type == "delegated":
        # EVO-65021: delegate the compile step to sandbox-mcp. Fail-fast on an
        # unreachable sandbox-mcp — NO silent local fallback (mirrors the
        # delegated clone backend, EVO-64616).
        wsm_cfg = getattr(config, "workspace_mcp", None)
        if wsm_cfg is None or not getattr(wsm_cfg, "endpoint", ""):
            raise RuntimeError(
                "scip_backend='delegated' requires workspace_mcp.endpoint to be "
                "configured (pointed at the sandbox-mcp service). None was "
                "provided."
            )
        _check_scip_backend_health(wsm_cfg.endpoint, wsm_cfg.api_key)
        indexer_map = ScipBackendFactory.create_indexer_map(
            "delegated",
            repo_root=Path(repo_root),
            workspace_mcp_config=wsm_cfg,
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
        )

    # "local" or any unrecognised value — default to the local subprocess map.
    logger.info("SCIPGenerator: using local subprocess indexers")
    return SCIPGenerator(
        Path(repo_root),
        max_workers=max_workers,
        workspace_id=workspace_id,
    )
