"""The pod-pull dispatch table (PR #1424) run by IndexJobClaimLoop.

Each executor reconstructs a memory-heavy index op from its claimed
background_jobs row's metadata and runs it on THIS (claiming) pod. Every
executor takes the row's metadata, a per-job progress_callback that the
claim loop routes to the shared row (so a work-stolen op reports incremental
progress to the originating node's dashboard instead of a 0 -> 100 jump),
and the claimed row's job id, which the indexing executors record on their
children's embedding_call_stats rows (Story #2118).

Built by ``build_index_dispatch`` (installed by startup/lifespan.py) so the
cluster dispatch path is one real, testable function.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.index_job_claim_loop import DispatchTable


def build_index_dispatch(golden_repo_manager: Any) -> DispatchTable:
    """The executor for every POD_PULL_OPS op, bound to this pod's manager."""
    from code_indexer.server.app_helpers import (
        _execute_repository_sync,
        build_sync_progress_webhook_callback,
    )
    from code_indexer.server.mcp.handlers.repos import (
        _provider_index_job,
        _provider_temporal_index_job,
    )

    def _pp_add_golden_repo(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return golden_repo_manager.execute_add_golden_repo_work(  # type: ignore[no-any-return]
            **md, progress_callback=progress_callback, job_id=job_id
        )

    def _pp_change_branch(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        # change_branch takes no job id; its re-index records
        # nojob:golden_repo_change_branch (as on the local pool).
        return golden_repo_manager.change_branch(  # type: ignore[no-any-return]
            md["alias"],
            md["target_branch"],
            progress_callback=progress_callback,
        )

    def _pp_sync_repository(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        # Rebuild the client webhook on THIS (executing) node from the
        # metadata options, then fan progress out to BOTH the DB-backed
        # dashboard callback and the webhook.
        options = md.get("options") or {}
        webhook_cb = build_sync_progress_webhook_callback(
            options.get("progress_webhook"),
            md["repo_id"],
            md["username"],
        )

        def _combined(progress: int) -> None:
            progress_callback(progress)
            if webhook_cb is not None:
                webhook_cb(progress)

        return _execute_repository_sync(  # type: ignore[no-any-return]
            repo_id=md["repo_id"],
            username=md["username"],
            options=options,
            activated_repo_manager=getattr(
                golden_repo_manager, "activated_repo_manager", None
            ),
            progress_callback=_combined,
        )

    def _pp_provider_index(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return _provider_index_job(
            **md, progress_callback=progress_callback, job_id=job_id
        )

    def _pp_provider_temporal(
        md: Dict[str, Any], progress_callback: Callable[..., Any], job_id: str
    ) -> Optional[Dict[str, Any]]:
        return _provider_temporal_index_job(
            **md, progress_callback=progress_callback, job_id=job_id
        )

    return {
        "add_golden_repo": _pp_add_golden_repo,
        "change_branch": _pp_change_branch,
        "sync_repository": _pp_sync_repository,
        "provider_index_add": _pp_provider_index,
        "provider_temporal_index_rebuild": _pp_provider_temporal,
    }
