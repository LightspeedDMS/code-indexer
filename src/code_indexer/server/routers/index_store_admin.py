"""Admin REST endpoint: the one-off IndexStore backfill after rollout.

``POST /api/admin/index-store/backfill`` brings golden pointers and activated
copies written before cross-pod serving existed to the published state
(``server/services/index_store_backfill.py``; runbook in
``docs/index-store-cross-pod.md``). Admin only, with TOTP elevation like the
other state-changing admin endpoints; ``dry_run`` (the default) only reports.

Golden refreshes are queued once cluster-wide (the job system dedups them);
activated copies are published by the pod that holds their index, so the
endpoint is meant to be called on EVERY pod -- or once with
``rebuild_missing`` to rebuild copies held nowhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import dependencies
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/index-store", tags=["index-store-admin"])


class BackfillRequest(BaseModel):
    dry_run: bool = True
    rebuild_missing: bool = False


class BackfillResponse(BaseModel):
    pod: str
    dry_run: bool
    golden_refreshed: Dict[str, Optional[str]]
    golden_skipped: Dict[str, str]
    golden_failed: Dict[str, str]
    activated_published: List[str]
    activated_rebuilt: Dict[str, str]
    activated_not_held_here: List[str]
    activated_failed: Dict[str, str]


def build_backfill(app_state: Any) -> Any:
    """The backfill over this server's managers (``app.state``)."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.services.index_store_backfill import (
        IndexStoreBackfill,
    )

    from code_indexer.server.services.index_store_backfill import refuse_if_local

    refuse_if_local(app_state.index_store)
    golden_repo_manager = app_state.golden_repo_manager
    arm = app_state.activated_repo_manager
    lifecycle = getattr(app_state, "global_lifecycle_manager", None)
    scheduler = getattr(lifecycle, "refresh_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="refresh scheduler not running")
    index_manager = getattr(arm, "_index_manager", None)
    if index_manager is None:
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        index_manager = ActivatedRepoIndexManager(
            activated_repo_manager=arm,
            background_job_manager=app_state.background_job_manager,
        )
    aliases = AliasManager(str(Path(golden_repo_manager.golden_repos_dir) / "aliases"))
    return IndexStoreBackfill(
        store=app_state.index_store,
        alias_records=aliases.list_records,
        golden_repos=lambda: {
            r["alias"]: r.get("repo_url", "")
            for r in golden_repo_manager.list_golden_repos()
        },
        refresh=lambda global_alias, submitter: scheduler.trigger_refresh_for_repo(
            global_alias, submitter_username=submitter, force_reset=True
        ),
        activated_repo_manager=arm,
        index_manager=index_manager,
    )


@router.post(
    "/backfill",
    response_model=BackfillResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def backfill_index_store(
    body: BackfillRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> BackfillResponse:
    """Publish what predates cross-pod serving (see the module docstring).

    Raises:
        HTTPException: 409 under the local IndexStore; 503 without a refresh
            scheduler; 500 when the registry cannot be read.
    """
    from code_indexer.server.services.index_store_backfill import (
        BackfillRefusedError,
    )

    try:
        backfill = build_backfill(request.app.state)
        report = backfill.run(
            submitter=current_user.username,
            dry_run=body.dry_run,
            rebuild_missing=body.rebuild_missing,
        )
    except BackfillRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("IndexStore backfill failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"backfill failed: {type(exc).__name__}: {exc}"
        ) from exc
    return BackfillResponse(**report.as_dict())
